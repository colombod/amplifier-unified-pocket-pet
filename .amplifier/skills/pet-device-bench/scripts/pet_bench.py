#!/usr/bin/env python3
"""USB bench driver for the pet's private bench build (PET_PRIVATE_MIC_DIAG=1).

Stdlib only. Counts and timings; never audio, names, addresses or identifiers.
  run   start a call from the console, stop it, save and summarise the report
  last  read the reports the pet kept for its last three calls
  scan  list the joined network's access points as the pet sees them
  parse summarise a saved report
The console node must be readable and writable by the caller. Opening it here
does not reset the pet (see Console)."""
import argparse
import json
import math
from pathlib import Path
import sys
import time

MIC = ('read read_errors read_max_us startup gated admitted queue_peak popped completed').split()
BENCH = ('send_count send_us_total send_us_max frame_count frame_us_total frame_us_max '
         'loop_us_max frames_waiting_peak playback_peak play_bytes play_skipped play_write_us_max').split()
TRANSPORT = ('write_wait_us_total write_wait_us_max write_wait_count write_us_total write_us_max '
             'read_hold_us_total read_hold_us_max read_hold_count idle_read_polls').split()
PLAYBACK = ('prebuffer_ms underruns underrun_ms_total underrun_ms_max '
            'u_lt100 u_lt200 u_lt400 u_lt800 u_lt1600 u_ge1600 arrivals arrival_gap_ms_max '
            'a_lt150 a_lt250 a_lt400 a_lt800 a_lt1600 a_ge1600').split()
WIFI = 'rssi_start rssi_min rssi_end channel second bandwidth is_11n'.split()
JITTER = ('320', '480', '640', '800', '960', '1280')
PREFIXES = ('pet-mic-v1 ', 'pet-bench-v1 ')
MAX_BYTES = 262144


def numbers(values, count):
    if len(values) != count or any(not v.isascii() or not v.isdecimal() for v in values):
        raise ValueError('invalid numeric report record')
    return list(map(int, values))


def parse_report(text):
    """Reject incomplete/mixed/duplicate reports; never infer success from an ack."""
    if len(text.encode('utf-8')) > MAX_BYTES:
        raise ValueError('report too large')
    mic, bench, tasks = {}, {}, []
    header = bh = transport = elapsed = status = None
    playback = wifi = None
    state = 'start'
    for line in text.splitlines():
        if not line.startswith(PREFIXES):
            continue
        p = line.split()
        if p in (['pet-bench-v1', 'voice', 'on'], ['pet-bench-v1', 'voice', 'off'], ['pet-bench-v1', 'voice', 'core'], ['pet-bench-v1', 'voice', 'prio'],
                 ['pet-bench-v1', 'voice', 'audio']):
            continue
        if p[:3] == ['pet-bench-v1', 'voice', 'jitter']:
            if len(p) != 4 or p[3] not in JITTER:
                raise ValueError('invalid jitter acknowledgement')
            continue
        if p[:3] == ['pet-bench-v1', 'voice', 'drop']:
            numbers(p[3:], 1)  # result code of leaving the access point
            continue
        if p[:3] == ['pet-bench-v1', 'voice', 'bw']:
            numbers(p[3:], 2)
            if p[3] not in ('20', '40'):
                raise ValueError('invalid bandwidth acknowledgement')
            continue
        key = tuple(p[:2])
        if key == ('pet-mic-v1', 'begin') and state == 'start':
            header = numbers(p[2:], 5)
            if not 1 <= header[0] <= 32:
                raise ValueError('unsupported bin count')
            state = 'mic'
        elif key == ('pet-mic-v1', 'bin') and state == 'mic':
            v = numbers(p[2:], 10)
            if v[0] in mic or v[0] >= header[0]:
                raise ValueError('duplicate/out-of-range mic bin')
            mic[v[0]] = dict(zip(MIC, v[1:]))
        elif p == ['pet-mic-v1', 'end'] and state == 'mic':
            state = 'between'
        elif key == ('pet-bench-v1', 'begin') and state == 'between':
            bh = numbers(p[2:], 2)
            if bh[0] != header[0]:
                raise ValueError('bin counts differ')
            state = 'bench'
        elif key == ('pet-bench-v1', 'bin') and state == 'bench':
            v = numbers(p[2:], 13)
            if v[0] in bench or v[0] >= bh[0]:
                raise ValueError('duplicate/out-of-range bench bin')
            bench[v[0]] = dict(zip(BENCH, v[1:]))
        elif key == ('pet-bench-v1', 'transport') and state == 'bench' and transport is None:
            transport = dict(zip(TRANSPORT, numbers(p[2:], 9)))
        elif key == ('pet-bench-v1', 'task') and state == 'bench':
            # FreeRTOS may name its timer task "Tmr Svc". The final three
            # numeric fields delimit the name without changing device names.
            name = ' '.join(p[2:-3])
            if len(p) < 6 or not name or len(name) > 31:
                raise ValueError('invalid task record')
            core = int(p[-3])
            if core not in (-1, 0, 1):
                raise ValueError('invalid core')
            priority, delta = numbers(p[-2:], 2)
            tasks.append(dict(name=name, core=core, priority=priority, delta_us=delta))
        elif key == ('pet-bench-v1', 'tasks_status') and state == 'bench' and status is None:
            status = numbers(p[2:], 3)
        elif key == ('pet-bench-v1', 'tasks_total') and state == 'bench' and elapsed is None:
            elapsed, = numbers(p[2:], 1)
        elif key == ('pet-bench-v1', 'playback') and state == 'bench' and playback is None:
            playback = dict(zip(PLAYBACK, numbers(p[2:], len(PLAYBACK))))
        elif key == ('pet-bench-v1', 'wifi') and state == 'bench' and wifi is None:
            wifi = dict(zip(WIFI, numbers(p[2:], len(WIFI))))
            for field in WIFI[:3]:
                wifi[field] = -wifi[field]  # 0 remains unavailable, not a fabricated dBm value
        elif p == ['pet-bench-v1', 'end'] and state == 'bench':
            state = 'done'
        else:
            raise ValueError('unexpected/duplicate report record')
    if (state != 'done' or header is None or len(mic) != header[0] or len(bench) != header[0]
            or transport is None or elapsed is None or status is None):
        raise ValueError('truncated report: require both complete blocks and pet-bench-v1 end')
    if not status[1] <= len(tasks) <= min(96, status[1] + 3):
        raise ValueError('missing/excess task records')
    bins = []
    duration = header[1] / 1000
    for i in sorted(mic):
        row = dict(index=i, **mic[i], **bench[i])
        span = min(1.0, max(0.0, duration - i))
        row['uplink_ratio'] = row['completed'] / (48000 * span) if span else None
        row['mean_send_ms'] = row['send_us_total'] / row['send_count'] / 1000 if row['send_count'] else 0
        row['max_send_ms'] = row['send_us_max'] / 1000
        row['mean_frame_ms'] = row['frame_us_total'] / row['frame_count'] / 1000 if row['frame_count'] else 0
        row['max_loop_ms'] = row['loop_us_max'] / 1000
        bins.append(row)
    clipped = bool(header[2] or bh[1])
    measured_seconds = min(duration, header[0])
    completed = sum(r['completed'] for r in bins)
    send_count = sum(r['send_count'] for r in bins)
    frame_count = sum(r['frame_count'] for r in bins)
    cpu_valid = bool(status[0] and status[1] and elapsed)
    for t in tasks:
        # 100% is one full core, not half the dual-core chip. -1 is unpinned;
        # migration cannot be attributed to an individual core from this API.
        t['cpu_percent_of_one_core'] = 100 * t['delta_us'] / elapsed if cpu_valid else None
    tasks.sort(key=lambda t: t['delta_us'], reverse=True)
    ratio = completed / (48000 * measured_seconds) if measured_seconds else None
    # Exclude first-bin startup discard and the partial terminal bin for diagnosis.
    steady = [b for b in bins if b['index'] >= 1 and b['index'] + 1 <= duration]
    steady_ratio = sum(b['completed'] for b in steady) / (48000 * len(steady)) if steady else ratio
    waits, writes = transport['write_wait_us_total'], transport['write_us_total']
    busy = next((t for t in tasks if not t['name'].lower().startswith(('idle', 'tidle'))), None)
    slow = steady_ratio is not None and steady_ratio < 0.95
    if steady_ratio is None:
        finding = 'No measurable uplink interval; no bottleneck conclusion.'
    elif not slow:
        finding = 'Healthy uplink in the measured window; no dominant bottleneck observed.'
    elif waits > writes and waits > measured_seconds * 100000:
        finding = 'Sender blocked on io lock is the dominant measured transport cost.'
    elif writes > waits and writes > measured_seconds * 100000:
        finding = 'Slow socket write is the dominant measured transport cost.'
    elif cpu_valid and busy and busy['cpu_percent_of_one_core'] >= 60:
        finding = 'CPU held by task %s is the leading starvation candidate (not causal proof).' % busy['name']
    elif sum(b['frame_us_total'] for b in bins) > measured_seconds * 100000:
        finding = 'Host frame handling is the dominant measured owner-loop cost.'
    else:
        finding = 'Sender slow; measured stages do not isolate a dominant cost.'
    if clipped or not cpu_valid or status[2]:
        finding += ' Coverage incomplete: clipped bins, unavailable CPU snapshots or vanished tasks; inspect coverage.'
    result = dict(duration_ms=header[1], clipped=clipped, residual_bytes=header[3], exit_reason=header[4],
                fifo_overflowed=header[4] == 2, bins=bins, uplink_ratio=ratio,
                steady_uplink_ratio=steady_ratio,
                mean_send_ms=sum(b['send_us_total'] for b in bins) / send_count / 1000 if send_count else 0,
                max_send_ms=max(b['send_us_max'] for b in bins) / 1000,
                mean_frame_ms=sum(b['frame_us_total'] for b in bins) / frame_count / 1000 if frame_count else 0,
                max_loop_ms=max(b['loop_us_max'] for b in bins) / 1000,
                transport=transport, tasks=tasks, tasks_elapsed_us=elapsed,
                coverage=dict(cpu_valid=cpu_valid, tasks_before=status[0], tasks_after=status[1], tasks_lost=status[2]),
                finding=finding)
    if playback is not None:
        result['playback'] = playback
    if wifi is not None:
        result['wifi'] = wifi
    return result


import re
_ANSI = re.compile(r'\x1b\[[0-9;]*m')
_LOG = re.compile(r'^([IWE]) \((\d+)\) ([\w.-]+): (.*)$')
_ADDR = re.compile(r'(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}|\b\d{1,3}(?:\.\d{1,3}){3}\b')


def status_line(line):
    """Fixed status words of the voice worker, and warning/error lines of any
    component with addresses removed. Never report content; kept privately."""
    m = _LOG.match(_ANSI.sub('', line).strip())
    if not m or (m.group(3) != 'pet_live' and m.group(1) == 'I'):
        return None
    return '%s %s %s: %s' % (m.group(1), m.group(2), m.group(3), _ADDR.sub('<addr>', m.group(4))[:160])


def collect(port, seconds, clock=time.monotonic, states=None, settle=0, core=None, prio=None,
            jitter=None, bw=None, audio=None):
    """Continuously drain, discard non-report lines, and always attempt mic-off.

    Port must already be opened with DTR/RTS disabled. No command is retried.
    A second call/report is rejected by parse_report rather than silently merged.
    """
    lines, pending = [], bytearray()
    kept = 0
    def drain():
        nonlocal kept
        data = port.read(4096)
        for b in data:
            if b == 10:
                if len(pending) <= 1024:
                    line = pending.decode('ascii', errors='replace').rstrip('\r')
                    kept_state = status_line(line) if states is not None else None
                    if kept_state and len(states) < 400:
                        states.append(kept_state)
                    if line.startswith(PREFIXES):
                        kept += len(line) + 1
                        if kept > MAX_BYTES:
                            raise ValueError('report too large')
                        lines.append(line)
                pending.clear()
            elif len(pending) < 1025:
                pending.append(b)
    # Opening the USB console resets the pet. Let it boot and rejoin Wi-Fi
    # before the one start command; nothing is sent while settling.
    if settle:
        settle_until = clock() + settle
        while clock() < settle_until:
            drain()
    started = clock()
    on_attempted = False
    def command(data):
        if port.write(data) != len(data):
            raise OSError('short console write; effect uncertain, command not retried')
    try:
        if core is not None:  # bench build only: placement of the next call's owner
            command(('voice core %s\n' % core).encode('ascii'))
        if prio is not None:
            command(('voice prio %s\n' % prio).encode('ascii'))
        if jitter is not None:
            command(('voice jitter %s\n' % jitter).encode('ascii'))
        if bw is not None:
            command(('voice bw %s\n' % bw).encode('ascii'))
        if audio is not None:  # '<5|6|7><a|0|1>': audio task priority and core
            command(('voice audio %s %s\n' % (audio[0], audio[1])).encode('ascii'))
        on_attempted = True
        command(b'voice on\n')
        while clock() - started < seconds:
            drain()
    finally:
        if on_attempted:
            command(b'voice off\n')
    # An overflow can report before N, and ordinary firmware retry policy may
    # already be closing a later call. A pre-off trailer must not terminate
    # the post-off drain and conceal a second report.
    ends_before_off = lines.count('pet-bench-v1 end')
    until = clock() + 20
    while lines.count('pet-bench-v1 end') == ends_before_off and clock() < until:
        drain()
    return '\n'.join(lines) + '\n'


class Console:
    """Raw USB console. The pet resets while DTR is released and RTS asserted,
    and every open asserts both: release both in ONE request, never in turn."""

    def __init__(self, path):
        import fcntl, os, select, struct, termios, tty
        self._os, self._select, self._termios = os, select, termios
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        fcntl.ioctl(self.fd, termios.TIOCMBIC, struct.pack('I', termios.TIOCM_DTR | termios.TIOCM_RTS))
        tty.setraw(self.fd)

    def read(self, count):
        if not self._select.select([self.fd], [], [], 0.05)[0]:
            return b''
        try:
            return self._os.read(self.fd, count)
        except BlockingIOError:
            return b''

    def write(self, data):
        return self._os.write(self.fd, data)

    def reset_input_buffer(self):
        self._termios.tcflush(self.fd, self._termios.TCIFLUSH)

    def close(self):
        self._os.close(self.fd)


def run(args, console=Console):
    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        raise ValueError('--out must be empty or absent')
    out.mkdir(parents=True, exist_ok=True)
    port = console(args.port)
    try:
        port.reset_input_buffer()  # drop stale reports, not a device/modem reset
        states = []
        try:
            text = collect(port, args.seconds, states=states, settle=getattr(args, 'settle', 0),
                           core=getattr(args, 'core', None), prio=getattr(args, 'prio', None),
                           jitter=getattr(args, 'jitter', None), bw=getattr(args, 'bw', None),
                           audio=getattr(args, 'audio', None))
        finally:
            (out / 'device-states.txt').write_text('\n'.join(states) + '\n', encoding='ascii', errors='replace')
    finally:
        port.close()
    (out / 'device-report.txt').write_text(text, encoding='ascii')
    summary = parse_report(text)  # never produce a success summary for truncation
    (out / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    return summary


def _lines(con, command, last_word, seconds):
    """Send one console command; the bench lines up to the one ending the answer."""
    con.reset_input_buffer()
    con.write(command)
    lines, pending, stop = [], bytearray(), time.monotonic() + seconds
    while time.monotonic() < stop:
        for b in con.read(4096):
            if b == 10:
                line = pending.decode('ascii', errors='replace').rstrip('\r')
                pending.clear()
                if line.startswith(PREFIXES):
                    lines.append(line)
                    if line.split()[1:2] == [last_word]:
                        return lines
            elif len(pending) < 1024:
                pending.append(b)
    raise ValueError('no complete answer to %r (bench build installed and idle?)' % command.decode().strip())


def kept_reports(lines_by_back):
    """Parse 'voice last <n>' answers. Lines before the header are stale console
    backlog, not the kept report; an answer without kept-end is rejected upstream."""
    out = []
    for back, lines in lines_by_back:
        at = next((i for i, l in enumerate(lines) if l.startswith('pet-bench-v1 kept ')), None)
        if at is None:
            raise ValueError('kept header missing')
        head = numbers(lines[at].split()[2:], 5)
        body = [l for l in lines[at + 1:] if not l.startswith('pet-bench-v1 kept')]
        info = dict(back=back, calls_since_boot=head[1], bytes=head[2],
                    seconds_ago=round((head[4] - head[3]) / 1000, 1) if head[2] else None)
        if body:
            info['report'] = '\n'.join(body) + '\n'
            info['summary'] = parse_report(info['report'])
        out.append(info)
    return out


def access_points(lines):
    """Parse a 'voice scan' answer: tag is a 16-bit hash, not an address."""
    aps = []
    for line in lines:
        p = line.split()
        if p[:2] == ['pet-bench-v1', 'ap']:
            v = numbers(p[2:], 5)
            aps.append(dict(tag=v[1], channel=v[2], rssi_dbm=-v[3], joined=bool(v[4])))
        elif p[:2] == ['pet-bench-v1', 'scan']:
            listed, code = numbers(p[2:], 2)
            if code:
                raise ValueError('the pet could not scan (result code %d); try again' % code)
            if listed != len(aps):
                raise ValueError('scan answer incomplete: %d of %d access points read' % (len(aps), listed))
    aps.sort(key=lambda a: a['rssi_dbm'], reverse=True)
    joined = next((a for a in aps if a['joined']), None)
    return dict(access_points=aps, joined=joined,
                on_strongest=bool(joined and aps and joined is aps[0]))


def last(args, console=Console):
    con = console(args.port)
    try:
        answers = [(back, _lines(con, ('voice last %d\n' % back).encode('ascii'), 'kept-end', 8)) for back in range(3)]
    finally:
        con.close()
    reports = kept_reports(answers)
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for r in reports:
            if 'report' in r:
                (out / ('last-%d.txt' % r['back'])).write_text(r['report'], encoding='ascii')
                (out / ('last-%d.json' % r['back'])).write_text(json.dumps(r['summary'], indent=2) + '\n')
    return [{k: v for k, v in r.items() if k != 'report'} for r in reports]


def scan(args, console=Console):
    con = console(args.port)
    try:
        return access_points(_lines(con, b'voice scan\n', 'scan', 15))
    finally:
        con.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('parse')
    p.add_argument('report', type=Path)
    r = sub.add_parser('run')
    r.add_argument('--port', required=True)
    r.add_argument('--seconds', type=float, required=True)
    r.add_argument('--out', required=True)
    r.add_argument('--settle', type=float, default=0)
    r.add_argument('--core', choices=('0', '1', 'any'))
    r.add_argument('--prio', choices=('4', '5', '6'))
    r.add_argument('--jitter', choices=JITTER)
    r.add_argument('--bw', choices=('20', '40'))
    r.add_argument('--audio', choices=[p + c for p in '567' for c in 'a01'])
    k = sub.add_parser('last')
    k.add_argument('--port', required=True)
    k.add_argument('--out')
    c = sub.add_parser('scan')
    c.add_argument('--port', required=True)
    args = parser.parse_args(argv)
    if args.command == 'run' and (not math.isfinite(args.seconds) or args.seconds <= 0):
        parser.error('--seconds must be finite and >0; prefer <=25 for the 32-bin window')
    try:
        result = (run(args) if args.command == 'run' else last(args) if args.command == 'last'
                  else scan(args) if args.command == 'scan'
                  else parse_report(args.report.read_text(encoding='ascii')))
    except (ValueError, OSError, ImportError) as exc:
        parser.exit(1, 'pet bench failed: %s\n' % exc)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
