import json
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import pet_bench as b

PLAYBACK = 'pet-bench-v1 playback 640 6 3300 1700 1 1 1 1 1 1 7 1800 1 1 1 1 1 1'
WIFI = 'pet-bench-v1 wifi 57 72 60 6 1 40 1'


def playback_report():
    return report().replace('pet-bench-v1 end', PLAYBACK + '\n' + WIFI + '\npet-bench-v1 end')


def temporary_output():
    return tempfile.TemporaryDirectory()


def report(kind='healthy'):
    completed = 48000 if kind == 'healthy' else 38400
    send_us = 20000 if kind == 'healthy' else 1200000
    wait = 1600000 if kind == 'lock' else 1000
    write = 1700000 if kind == 'sender' else 20000
    busy = 2600000 if kind == 'cpu' else 120000
    lines = ['discard me', 'pet-bench-v1 voice on', 'pet-mic-v1 begin 3 3000 0 0 8']
    for i in range(3):
        lines.append('pet-mic-v1 bin %d 48000 0 20000 0 0 48000 %d 48000 %d' %
                     (i, 960 if kind == 'healthy' else (i + 1) * 9600, completed))
    lines += ['pet-mic-v1 end', 'pet-bench-v1 begin 3 0']
    for i in range(3):
        lines.append('pet-bench-v1 bin %d 10 %d %d 20 4000 200 5000 2 15360 48000 960 20000' %
                     (i, send_us, send_us // 10))
    lines += ['pet-bench-v1 transport %d %d 30 %d %d 30000 1000 30 100' %
              (wait, wait // 30, write, write // 30),
              'pet-bench-v1 task wifi 0 23 %d' % busy,
              'pet-bench-v1 task IDLE1 1 0 2800000',
              'pet-bench-v1 tasks_status 2 2 0',
              'pet-bench-v1 tasks_total 3000000', 'pet-bench-v1 end']
    return '\n'.join(lines) + '\n'


class ReportTests(unittest.TestCase):
    def test_playback_wifi(self):
        s = b.parse_report(playback_report())
        self.assertEqual(s['playback'], dict(zip(b.PLAYBACK, [640, 6, 3300, 1700] + [1]*6 + [7, 1800] + [1]*6)))
        self.assertEqual(s['wifi'], dict(zip(b.WIFI, [-57, -72, -60, 6, 1, 40, 1])))

    def test_optional_records(self):
        s = b.parse_report(report())
        self.assertNotIn('playback', s)
        self.assertNotIn('wifi', s)
        for line, key in ((PLAYBACK, 'playback'), (WIFI, 'wifi')):
            s = b.parse_report(report().replace('pet-bench-v1 end', line + '\npet-bench-v1 end'))
            self.assertIn(key, s)
        s = b.parse_report(playback_report().replace('wifi 57 72 60', 'wifi 0 0 0'))
        self.assertEqual([s['wifi'][k] for k in b.WIFI[:3]], [0, 0, 0])

    def test_invalid_new_records(self):
        for line in (PLAYBACK, WIFI):
            for bad in (' '.join(line.split()[:-1]), line + '\n' + line, line + ' 0',
                        line.replace(' 1', ' -1', 1)):
                with self.subTest(line=line, bad=bad), self.assertRaises(ValueError):
                    b.parse_report(playback_report().replace(line, bad))

    def test_new_acknowledgements(self):
        for jitter in b.JITTER:
            for bw in ('20', '40'):
                for rc in ('0', '12289'):
                    ack = f'pet-bench-v1 voice jitter {jitter}\npet-bench-v1 voice bw {bw} {rc}\n'
                    self.assertIn('playback', b.parse_report(ack + playback_report()))
        for ack in ('voice jitter', 'voice jitter 400', 'voice jitter 640 0', 'voice bw 20',
                    'voice bw 80 0', 'voice bw 20 nope', 'voice bw 40 0 0'):
            with self.subTest(ack=ack), self.assertRaises(ValueError):
                b.parse_report('pet-bench-v1 ' + ack + '\n' + report())

    def test_new_cli_console_order_and_summary(self):
        # Full CLI -> run -> collect with an in-memory port and synthetic clock.
        real_run, real_collect = b.run, b.collect
        class Port:
            commands = None
            def __init__(self, path):
                self.commands = []
                self.closed = False
            def reset_input_buffer(self):
                pass
            def write(self, data):
                self.commands.append(data)
                return len(data)
            def read(self, size):
                data = getattr(self, 'data', playback_report().encode())
                self.data = b''
                return data
            def close(self):
                self.closed = True
        for jitter, bw in (('320', '20'), ('1280', '40')):
            with temporary_output() as directory:
                port = Port('FAKE-NOT-A-DEVICE')
                ticks = iter([0, .1, 1.1, 1.2, 21.3])
                def collect(*args, **kwargs):
                    return real_collect(*args, **kwargs, clock=lambda: next(ticks))
                with patch.object(b, 'collect', side_effect=collect), \
                     patch.object(b, 'run', side_effect=lambda args: real_run(args, console=lambda path: port)), \
                     contextlib.redirect_stdout(io.StringIO()):
                    b.main(['run', '--port', 'FAKE-NOT-A-DEVICE', '--seconds', '1', '--out', directory,
                            '--core', '1', '--prio', '6', '--jitter', jitter, '--bw', bw])
                self.assertEqual(port.commands, [b'voice core 1\n', b'voice prio 6\n',
                                                f'voice jitter {jitter}\n'.encode(), f'voice bw {bw}\n'.encode(),
                                                b'voice on\n', b'voice off\n'])
                self.assertTrue(port.closed)
                saved = json.loads((Path(directory) / 'summary.json').read_text())
                self.assertEqual(saved['wifi']['rssi_min'], -72)
                self.assertEqual(saved['playback']['prebuffer_ms'], 640)
        for option, value in (('--jitter', '400'), ('--bw', '80')):
            with patch.object(b, 'run') as run, contextlib.redirect_stderr(io.StringIO()), \
                 self.assertRaises(SystemExit):
                b.main(['run', '--port', 'FAKE', '--seconds', '1', '--out', 'UNUSED', option, value])
            run.assert_not_called()

    def test_kept_reports(self):
        body = [l for l in report().splitlines() if l.startswith(b.PREFIXES) and 'voice on' not in l]
        answer = ['pet-bench-v1 end', 'pet-bench-v1 kept 0 2 5527 92911 300881'] + body + ['pet-bench-v1 kept-end']
        empty = ['pet-bench-v1 kept 1 2 0 0 300900', 'pet-bench-v1 kept-end']
        kept = b.kept_reports([(0, answer), (1, empty)])
        self.assertEqual((kept[0]['calls_since_boot'], kept[0]['seconds_ago']), (2, 208.0))
        self.assertEqual(kept[0]['summary']['exit_reason'], 8)
        self.assertNotIn('summary', kept[1])
        self.assertIsNone(kept[1]['seconds_ago'])
        with self.assertRaises(ValueError):
            b.kept_reports([(0, body)])
        with self.assertRaises(ValueError):   # truncated kept report: no trailer inside
            b.kept_reports([(0, ['pet-bench-v1 kept 0 1 900 1 2'] + body[:-1])])

    def test_access_points(self):
        lines = ['pet-bench-v1 ap 0 16258 11 74 1', 'pet-bench-v1 ap 1 21947 1 42 0', 'pet-bench-v1 scan 2 0']
        got = b.access_points(lines)
        self.assertEqual([a['rssi_dbm'] for a in got['access_points']], [-42, -74])
        self.assertEqual(got['joined']['channel'], 11)
        self.assertFalse(got['on_strongest'])
        self.assertTrue(b.access_points(['pet-bench-v1 ap 0 1 1 42 1', 'pet-bench-v1 scan 1 0'])['on_strongest'])
        for bad in (['pet-bench-v1 ap 0 1 1 42 1', 'pet-bench-v1 scan 2 0'], ['pet-bench-v1 scan 0 12290']):
            with self.assertRaises(ValueError):
                b.access_points(bad)

    def test_last_and_scan_use_one_console_and_close_it(self):
        class Port:
            def __init__(self, answer):
                self.data = bytearray(); self.answer = answer; self.sent = []; self.closed = False
            def reset_input_buffer(self):
                self.data.clear()
            def write(self, data):
                self.sent.append(data); self.data += self.answer(data); return len(data)
            def read(self, count):
                out = bytes(self.data[:count]); del self.data[:count]; return out
            def close(self):
                self.closed = True
        port = Port(lambda d: b'noise\r\npet-bench-v1 ap 0 7 6 50 1\r\npet-bench-v1 scan 1 0\r\n')
        got = b.scan(types.SimpleNamespace(port='FAKE'), console=lambda path: port)
        self.assertTrue(got['on_strongest'] and port.closed)
        self.assertEqual(port.sent, [b'voice scan\n'])
        port = Port(lambda d: ('pet-bench-v1 kept %s 0 0 0 5\npet-bench-v1 kept-end\n' % d.decode().split()[2]).encode())
        got = b.last(types.SimpleNamespace(port='FAKE', out=None), console=lambda path: port)
        self.assertEqual([r['back'] for r in got], [0, 1, 2])
        self.assertEqual(port.sent, [b'voice last 0\n', b'voice last 1\n', b'voice last 2\n'])
        self.assertTrue(port.closed)

    def test_healthy(self):
        s = b.parse_report(report())
        self.assertAlmostEqual(s['uplink_ratio'], 1)
        self.assertEqual(s['mean_send_ms'], 2)
        self.assertEqual(s['max_send_ms'], 2)
        self.assertEqual(s['mean_frame_ms'], .2)
        self.assertEqual(s['max_loop_ms'], 5)
        self.assertEqual(s['bins'][1]['queue_peak'], 960)
        self.assertEqual(s['bins'][1]['play_skipped'], 960)
        self.assertFalse(s['fifo_overflowed'])
        self.assertIn('Healthy', s['finding'])
        self.assertEqual(s['tasks'][0]['name'], 'IDLE1')
        self.assertAlmostEqual(s['tasks'][0]['cpu_percent_of_one_core'], 93.33333333)

    def test_sender_slow(self):
        s = b.parse_report(report('sender'))
        self.assertAlmostEqual(s['steady_uplink_ratio'], .8)
        self.assertEqual(s['mean_send_ms'], 120)
        self.assertIn('Slow socket write', s['finding'])

    def test_lock_dominated(self):
        s = b.parse_report(report('lock'))
        self.assertEqual(s['transport']['write_wait_us_total'], 1600000)
        self.assertIn('blocked on io lock', s['finding'])

    def test_cpu_starved(self):
        s = b.parse_report(report('cpu'))
        self.assertIn('CPU held by task wifi', s['finding'])
        self.assertAlmostEqual(s['tasks'][1]['cpu_percent_of_one_core'], 86.66666667)

    def test_truncated(self):
        with self.assertRaisesRegex(ValueError, 'truncated'):
            b.parse_report(report().replace('pet-bench-v1 end\n', ''))

    def test_duplicate_or_missing_bin(self):
        r = report()
        with self.assertRaises(ValueError):
            b.parse_report(r + r)
        with self.assertRaises(ValueError):
            b.parse_report('\n'.join(line for line in r.splitlines() if not line.startswith('pet-bench-v1 bin 1 ')))

    def test_missing_tasks(self):
        with self.assertRaisesRegex(ValueError, 'task records'):
            b.parse_report('\n'.join(line for line in report().splitlines()
                                     if not line.startswith('pet-bench-v1 task ')))

    def test_task_name_with_spaces(self):
        s = b.parse_report(report('cpu').replace('task wifi ', 'task Tmr Svc '))
        self.assertIn('CPU held by task Tmr Svc', s['finding'])
        self.assertEqual(s['tasks'][1]['name'], 'Tmr Svc')

    def test_overflow_clipping_and_cpu_unavailable(self):
        r = report('lock').replace('3000 0 0 8', '3000 1 0 2').replace('tasks_status 2 2 0', 'tasks_status 0 0 1')
        s = b.parse_report(r)
        self.assertTrue(s['fifo_overflowed'])
        self.assertTrue(s['clipped'])
        self.assertFalse(s['coverage']['cpu_valid'])
        self.assertIsNone(s['tasks'][0]['cpu_percent_of_one_core'])
        self.assertIn('Coverage incomplete', s['finding'])

    def test_collect_filters_and_stops(self):
        class Port:
            def __init__(self):
                self.commands = []
                self.data = [b'private discarded\n' + report().encode(), b'']
            def write(self, data):
                self.commands.append(data)
                return len(data)
            def read(self, size):
                return self.data.pop(0) if self.data else b''
        p = Port()
        ticks = iter([0, .1, 1.1, 1.2, 21.3])
        text = b.collect(p, 1, clock=lambda: next(ticks))
        self.assertNotIn('discard', text)
        self.assertEqual(p.commands, [b'voice on\n', b'voice off\n'])
        self.assertEqual(b.parse_report(text)['uplink_ratio'], 1)

    def test_early_report_does_not_conceal_later_call(self):
        class Port:
            data = [report().encode(), report().encode()]
            def write(self, data):
                return len(data)
            def read(self, size):
                return self.data.pop(0)
        ticks = iter([0, .1, 1.1, 1.2, 1.3])
        text = b.collect(Port(), 1, clock=lambda: next(ticks))
        with self.assertRaises(ValueError):
            b.parse_report(text)

    def test_off_on_read_failure(self):
        class Port:
            commands = []
            def write(self, data):
                self.commands.append(data)
                return len(data)
            def read(self, size):
                raise OSError('synthetic read error')
        p = Port()
        with self.assertRaises(OSError):
            b.collect(p, 1, clock=lambda: 0)
        self.assertEqual(p.commands, [b'voice on\n', b'voice off\n'])

    def test_run_uses_one_console_and_closes_it(self):
        class Port:
            def __init__(self, path):
                self.path = path
            def close(self):
                self.closed = True
            def reset_input_buffer(self):
                self.drained = True
        ports = []
        def console(path):
            ports.append(Port(path))
            return ports[-1]
        with temporary_output() as directory:
            with patch.object(b, 'collect', return_value=report()):
                s = b.run(types.SimpleNamespace(port='FAKE-NOT-A-DEVICE', seconds=1, out=directory), console=console)
            self.assertEqual([p.path for p in ports], ['FAKE-NOT-A-DEVICE'])
            self.assertTrue(ports[0].closed)
            self.assertTrue(ports[0].drained)
            self.assertEqual(json.loads((Path(directory) / 'summary.json').read_text()), s)
            self.assertTrue((Path(directory) / 'device-report.txt').is_file())


if __name__ == '__main__':
    unittest.main()
