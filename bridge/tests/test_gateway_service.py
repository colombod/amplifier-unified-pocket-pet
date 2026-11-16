"""Always-on gateway checks. All socket entry points are forbidden by the bases."""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import io
import json
import os
import shutil
import stat
import tempfile
import threading
from pathlib import Path
from unittest import mock

from pet_bridge import cli, core
from pet_bridge import gateway_core as gc
from pet_bridge import gateway_events as ge
from pet_bridge import gateway_server as gsv
from pet_bridge.journal import BINDING_NAME

from . import authfakes as af
from . import fakes
from . import gatewayfakes as gf
from . import test_companion as tc
from . import test_gateway_flow as flow
from . import test_gateway_server as srv
from .test_gateway_core import audio_frame, delegation, delta, until

NEW_INSTANCE = "87654321-1234-4234-8234-123456789abc"


class RestartClient:
    def __init__(self):
        self.instance = af.INSTANCE
        self.identity = af.HOST_IDENTITY
        self.unreachable = False
        self.calls = []
        self.attach_gate = None
        self.attach_entered = threading.Event()
        self.send_gate = None
        self.send_entered = threading.Event()

    def request(self, origin, route, *, headers=None, body=b""):
        self.calls.append((route.name, route.method, headers, body,
                           threading.current_thread().name))
        if route.name == "attach":
            self.attach_entered.set()
            if self.attach_gate is not None:
                if not self.attach_gate.wait(5):
                    raise AssertionError("test gate timed out")
            if self.unreachable:
                raise OSError(gf.TEXT_CANARY)
            return af.json_response(af.attach_ok(
                hostInstanceId=self.instance, hostIdentity=self.identity
            ))
        if route.name == "companion":
            return tc.lpage([tc.srow(srv.SESSION_ID)], hostInstanceId=self.instance,
                            hostIdentity=self.identity)
        if route.name == "send":
            self.send_entered.set()
            if self.send_gate is not None and not self.send_gate.wait(5):
                raise AssertionError("test send gate timed out")
            command = json.loads(body)["id"]
            return af.json_response(json.loads(fakes.accepted_body(srv.SESSION_ID, command)))
        raise AssertionError("unexpected offline route")

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


class ProbeSession:
    """Exercise real guarded core calls, with no voice/provider involved."""

    def __init__(self, case):
        self.case = case
        self.outcome = gc.SessionOutcome("CLOSED", False, None, None, None, True, 0, 0, 0)

    async def request_shutdown(self):
        pass

    async def run(self):
        try:
            await self._run()
        except Exception as err:
            self.case.errors.append(err)
            raise

    async def _run(self):
        c = self.case
        await c.held.runner.run("resume", c.held.ops.resume, retain=False)
        # Several Unified calls in ONE session must not reattach.
        c.preflights.append(await c.held.runner.run(
            "preflight", c.held.ops.preflight, retain=False))
        c.preflights.append(await c.held.runner.run(
            "preflight", c.held.ops.preflight, retain=False))
        if c.send:
            turn = await c.held.runner.run("begin", c.held.ops.begin, retain=False)
            await c.held.runner.run("stage", lambda: c.held.ops.stage(
                turn, [(f"canary-event-{len(c.results)}", gf.TEXT_CANARY)]), retain=False)
            preview = await c.held.runner.run(
                "seal", lambda: c.held.ops.seal(turn), retain=False)
            result = await c.held.runner.run("confirm", lambda: c.held.ops.confirm(
                turn, preview.text_sha256, 10), retain=False)
            c.results.append(result)
            await c.held.runner.run("stop", c.held.ops.stop, retain=False)


class SessionFollowTest(gf.OfflineAsyncCase):
    async def asyncSetUp(self):
        self.base = Path(os.path.realpath(tempfile.mkdtemp(prefix="pet-service-")))
        self.addCleanup(shutil.rmtree, self.base)
        self.fx = srv.StateFixture(self.base)
        self.path = self.fx.build()
        self.client = RestartClient()
        self.spies = srv.Spies()
        self.spies.unified = self.client
        self.clock = gf.FakeLoopClock()
        self.held = gsv._Held()
        self.addAsyncCleanup(gsv._release, self.held)
        await gsv._startup(self.held, self.path, {}, io.StringIO(),
                           self.spies.deps(self.clock))
        self.gw = self.held.gateway
        self.sockets = []
        self.preflights, self.results, self.send = [], [], False
        self.errors = []

        def ws():
            result = srv.FakeServerWS()
            self.sockets.append(result)
            return result

        self.gw._ws_factory = ws
        self.gw._device_factory = lambda *_: gf.FakeDeviceLink()
        self.gw._session_factory = lambda *_: ProbeSession(self)

    async def session(self):
        response = await asyncio.wait_for(self.gw.handle(srv.request_for()), 5)
        await until(lambda: not self.gw.busy)
        self.assertEqual(self.errors, [])
        return response

    async def test_two_sessions_follow_restart_and_second_sends_on_new_journal_binding(self):
        path = self.base / "events"
        sink = ge.EventLog(path, io.StringIO())
        self.addCleanup(sink.close)
        self.gw._event_log = sink
        self.send = True
        before = self.held.journal.get(BINDING_NAME)
        self.assertTrue((await self.session()).prepared)
        self.client.instance = NEW_INSTANCE
        self.assertTrue((await self.session()).prepared)
        self.assertTrue(all(p.idle for p in self.preflights))
        self.assertEqual([r.outcome for r in self.results], ["accepted", "accepted"])
        after = self.held.journal.get(BINDING_NAME)
        self.assertEqual(after["binding"]["host_instance_id"], NEW_INSTANCE)
        self.assertGreater(after["epoch"], before["epoch"])
        for field in ("host_id", "client_id", "session_id", "credential_ref"):
            self.assertEqual(after["binding"][field], before["binding"][field])
        self.assertEqual(self.held.ops._binding.host_instance_id, NEW_INSTANCE)
        attaches = self.client.named("attach")
        self.assertEqual(len(attaches), 3)  # startup + exactly one per session
        self.assertEqual(len(self.client.named("send")), 2)
        for call in attaches:
            self.assertEqual(call[1], "POST")
            self.assertEqual(call[2]["Authorization"], "Bearer " + af.TOKEN)
            self.assertEqual(json.loads(call[3])["clientId"], af.CLIENT_ID)
        self.assertEqual([c[4] for c in attaches[1:]], ["pet-core-runner"] * 2)
        changes = [json.loads(line) for line in path.read_text().splitlines()
                   if json.loads(line)["event"] == "host_restart_follow"]
        self.assertEqual(len(changes), 1)
        self.assertEqual((changes[0]["old_instance"], changes[0]["new_instance"]),
                         (af.INSTANCE[:8], NEW_INSTANCE[:8]))
        self.assertNotIn(NEW_INSTANCE, path.read_text())
        self.assertNotIn(af.HOST_IDENTITY, path.read_text())

    async def test_wrong_identity_refuses_only_that_session_then_recovers(self):
        before = self.held.journal.get(BINDING_NAME)
        self.client.identity = "6" * 64
        self.client.instance = NEW_INSTANCE
        response = await self.session()
        self.assertEqual((response.status, response.body),
                         (503, b'{"error":"core_binding_required"}'))
        self.assertFalse(self.sockets[-1].prepared)
        self.assertEqual(self.gw.sessions_started, 0)
        self.assertEqual(self.held.journal.get(BINDING_NAME), before)
        self.assertEqual(self.client.named("send"), [])
        self.assertEqual(self.spies.provider_calls, 0)
        self.client.identity = af.HOST_IDENTITY
        self.assertTrue((await self.session()).prepared)
        self.assertTrue(self.preflights[-1].idle)
        self.assertEqual(len(self.client.named("attach")), 3)

    async def test_unreachable_follow_proceeds_on_unchanged_stored_pin(self):
        before = self.held.journal.get(BINDING_NAME)
        self.client.unreachable = True  # only attach is unreachable in this fake
        self.assertTrue((await self.session()).prepared)
        self.assertEqual(self.held.journal.get(BINDING_NAME)["binding"], before["binding"])
        self.assertEqual(self.held.journal.get(BINDING_NAME)["epoch"], before["epoch"])
        self.assertEqual(self.held.ops._binding.host_instance_id, af.INSTANCE)
        self.assertTrue(all(p.idle for p in self.preflights))
        self.assertEqual(len(self.client.named("attach")), 2)
        self.client.instance = NEW_INSTANCE  # stale live pin still fails closed
        await self.session()
        self.assertFalse(self.preflights[-1].idle)
        self.assertEqual(self.preflights[-1].reason, "companion_host_mismatch")
        self.assertEqual(self.held.store.load_attachment(af.CRED_REF).host_instance_id,
                         af.INSTANCE)
        self.assertEqual(len(self.client.named("attach")), 3)

    async def test_follow_does_not_resolve_or_replay_uncertain_command(self):
        name = "cmd-12345678-1234-4234-8234-123456789abc.json"
        self.held.journal.put(name, {
            "state": "UNCERTAIN", "binding": self.held.journal.get(BINDING_NAME)["binding"],
            "epoch": 0, "created_at": 1.0, "updated_at": 1.0,
            "event_ids": [], "reason": "transport_exception",
        })
        path = self.base / "state/journal" / name
        before = path.read_bytes()
        self.client.instance = NEW_INSTANCE
        await self.session()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.client.named("send"), [])
        self.assertEqual(self.client.named("detail"), [])

    async def test_follow_queues_behind_pending_request_and_busy_refuses_without_attach(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def pending():
            entered.set()
            release.wait(5)

        job = self.held.runner.submit("confirm", pending, retain=False)
        await until(entered.is_set)
        task = asyncio.create_task(self.gw.handle(srv.request_for()))
        await until(lambda: self.gw._follow_task is not None)
        self.assertEqual(len(self.client.named("attach")), 1)
        self.assertEqual((await self.gw.handle(srv.request_for())).status, 409)
        self.assertEqual(len(self.client.named("attach")), 1)
        release.set()
        await job.wait()
        self.assertTrue((await task).prepared)
        await until(lambda: not self.gw.busy)
        self.assertEqual(len(self.client.named("attach")), 2)

    async def test_cancelled_follow_keeps_slot_and_shutdown_waits_for_worker(self):
        self.client.attach_entered.clear()
        release = self.client.attach_gate = threading.Event()
        self.addCleanup(release.set)
        task = asyncio.create_task(self.gw.handle(srv.request_for()))
        await until(self.client.attach_entered.is_set)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.gw.busy)
        self.assertEqual((await self.gw.handle(srv.request_for())).status, 409)
        shutdown = asyncio.create_task(self.gw.shutdown())
        await asyncio.sleep(0)
        self.assertFalse(shutdown.done())
        release.set()
        await shutdown
        await until(lambda: not self.gw.busy)
        self.assertEqual(self.gw.sessions_started, 0)
        self.assertFalse(self.sockets[0].prepared)
        self.assertEqual(len(self.client.named("attach")), 2)

    async def test_host_restart_during_request_is_not_followed_until_next_session(self):
        self.send = True
        release = self.client.send_gate = threading.Event()
        self.addCleanup(release.set)
        task = asyncio.create_task(self.gw.handle(srv.request_for()))
        await until(self.client.send_entered.is_set)
        self.client.instance = NEW_INSTANCE
        self.assertEqual((await self.gw.handle(srv.request_for())).status, 409)
        self.assertEqual(len(self.client.named("attach")), 2)  # startup + first session
        self.assertEqual(self.held.ops._binding.host_instance_id, af.INSTANCE)
        release.set()
        await task
        await until(lambda: not self.gw.busy)
        self.assertEqual(len(self.client.named("attach")), 2)
        await self.session()
        self.assertEqual(len(self.client.named("attach")), 3)
        self.assertEqual(self.held.ops._binding.host_instance_id, NEW_INSTANCE)

    async def test_follow_cannot_change_any_stable_binding_field(self):
        old = self.held.ops._binding
        for field, value in (("host_id", "6" * 64), ("session_id", "other-session"),
                             ("client_id", "D" * 32),
                             ("credential_ref", "device-" + "cd" * 16)):
            with self.subTest(field=field), self.assertRaises(core.BridgeError) as ctx:
                await self.held.runner.run(
                    "follow_host_restart",
                    lambda: self.held.ops.follow_host_restart(
                        lambda binding: dataclasses.replace(binding, **{field: value})),
                    retain=False,
                )
            self.assertEqual(ctx.exception.code, "core_binding_required")
            self.assertIs(self.held.ops._binding, old)
        self.assertEqual(len(self.client.named("attach")), 1)  # startup only


class EventFileTest(srv.ServerOfflineCase):
    def open_log(self, path=None):
        log = ge.EventLog(path or self.base / "events.jsonl", io.StringIO())
        self.addCleanup(log.close)
        return log

    def test_create_0600_append_existing_and_utc_timestamp(self):
        path = self.base / "events.jsonl"
        log = self.open_log()
        log.write("gateway_start")
        log.close()
        log = self.open_log()
        log.write("gateway_stop", code=None)
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual([r["event"] for r in rows], ["gateway_start", "gateway_stop"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        for row in rows:
            self.assertEqual(datetime.datetime.fromisoformat(row["timestamp"]).utcoffset(),
                             datetime.timedelta(0))

    def test_unsafe_paths_are_fixed_errors_without_content_or_permission_changes(self):
        loose = self.write("loose", gf.TEXT_CANARY, 0o644)
        link = self.base / "link"
        link.symlink_to(loose)
        hardlink = self.base / "hardlink"
        os.link(loose, hardlink)
        fifo = self.base / "fifo"
        os.mkfifo(fifo, 0o600)
        public = self.base / "public"
        public.mkdir(mode=0o755)
        parent_link = self.base / "parent-link"
        parent_link.symlink_to(self.base, target_is_directory=True)
        for path in (Path("relative"), self.base / "missing/events", loose, link,
                     hardlink, fifo, self.base, public / "events",
                     parent_link / "events"):
            with self.subTest(path=path), self.assertRaises(ge.EventLogError) as ctx:
                self.open_log(path)
            self.assertEqual(str(ctx.exception), "event_log_invalid")
        self.assertEqual(loose.read_text(), gf.TEXT_CANARY)
        self.assertEqual(stat.S_IMODE(loose.stat().st_mode), 0o644)

    def test_runtime_write_failure_warns_once_and_disables_without_raising(self):
        err = io.StringIO()
        log = ge.EventLog(self.base / "events", err)
        self.addCleanup(log.close)
        with mock.patch.object(ge.os, "write", side_effect=OSError(gf.KEY_CANARY)):
            log.write("gateway_start")
            log.write("gateway_stop", code=None)
        self.assertEqual(err.getvalue(), "error: event_log_failed\n")

    def test_unknown_event_or_additional_content_field_cannot_be_logged(self):
        log = self.open_log()
        with self.assertRaises(ValueError):
            log.write("gateway_start", transcript=gf.TEXT_CANARY)
        with self.assertRaises(ValueError):
            log.write(gf.TEXT_CANARY)
        self.assertEqual((self.base / "events.jsonl").read_bytes(), b"")

    def test_cli_option_is_forwarded_and_relative_path_is_usage_without_start(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(gsv, "serve_main", return_value=0) as serve:
            self.assertEqual(cli.main(
                ["serve", "--config", "/private/config.json", "--event-log", "/private/events"],
                env={}, stdout=out, stderr=err), 0)
            self.assertEqual(serve.call_args.kwargs["event_log_path"], Path("/private/events"))
            serve.reset_mock()
            self.assertEqual(cli.main(
                ["serve", "--config", "/private/config.json", "--event-log", "relative"],
                env={}, stdout=out, stderr=err), 2)
            serve.assert_not_called()
        self.assertNotIn("relative", err.getvalue())

    def test_unit_has_required_placeholders_and_service_safety_settings(self):
        unit = (Path(gsv.__file__).parents[1] / "deploy/pocket-gateway.service").read_text()
        for expected in ("Type=simple", "After=network-online.target",
                         "Restart=on-failure", "RestartSec=10s", "WantedBy=default.target",
                         "WorkingDirectory=@BRIDGE_DIR@", "PYTHONDONTWRITEBYTECODE=1",
                         '@PYTHON@ -B -m pet_bridge serve --config "@CONFIG@"',
                         '--event-log "@EVENT_LOG@"'):
            self.assertIn(expected, unit)
        self.assertNotIn(af.TOKEN, unit)
        self.assertNotIn(gf.KEY_CANARY, unit)


class ServeEventsTest(gf.OfflineAsyncCase):
    async def asyncSetUp(self):
        self.base = Path(os.path.realpath(tempfile.mkdtemp(prefix="pet-events-")))
        self.addCleanup(shutil.rmtree, self.base)
        self.fx = srv.StateFixture(self.base)
        self.path = self.fx.build()
        self.spies, self.clock = srv.Spies(), gf.FakeLoopClock()
        self.out, self.err = io.StringIO(), io.StringIO()

    async def serve(self, log=None):
        return await gsv._serve(self.path, {}, self.out, self.err,
                                self.spies.deps(self.clock), log)

    async def test_no_flag_never_opens_log_or_writes_additional_file(self):
        before = set(self.base.iterdir())
        with mock.patch.object(gsv, "EventLog", side_effect=AssertionError("not optional")):
            self.assertEqual(await self.serve(), 0)
        self.assertEqual(set(self.base.iterdir()), before)

    async def test_invalid_log_fails_before_config_host_or_listener(self):
        self.assertEqual(await self.serve(self.base / "missing/events"), 1)
        self.assertEqual(self.err.getvalue(), "error: event_log_invalid\n")
        self.assertEqual(self.spies.client_calls, 0)
        self.assertEqual(self.spies.site_calls, [])

    async def test_unwritable_log_fails_before_listen_even_when_open_succeeds(self):
        with mock.patch.object(ge.os, "write", side_effect=OSError(gf.KEY_CANARY)):
            self.assertEqual(await self.serve(self.base / "events"), 1)
        self.assertEqual(self.err.getvalue(), "error: event_log_invalid\n")
        self.assertEqual(self.spies.client_calls, 0)
        self.assertEqual(self.spies.site_calls, [])

    async def test_start_stop_and_listen_failure_are_fixed_metadata(self):
        log = self.base / "events"
        self.spies.site_error = OSError(98, gf.TEXT_CANARY)  # EADDRINUSE, no real bind
        self.assertEqual(await self.serve(log), 1)
        self.assertEqual(self.err.getvalue(), "error: listen_failed\n")
        rows = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual([r["event"] for r in rows], ["gateway_start", "gateway_stop"])
        self.assertEqual(rows[-1]["code"], "listen_failed")
        self.assertNotIn(gf.TEXT_CANARY, log.read_text())

    async def test_log_startup_follow_and_provider_failures_have_fixed_fields(self):
        self.spies.unified = af.FakeClient(
            af.json_response(af.attach_ok(hostInstanceId=NEW_INSTANCE)))
        log = self.base / "events"
        self.assertEqual(await self.serve(log), 0)
        rows = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(rows[1], {
            "timestamp": rows[1]["timestamp"], "event": "host_restart_follow",
            "old_instance": af.INSTANCE[:8], "new_instance": NEW_INSTANCE[:8],
        })
        # The closed gateway still exposes the production dial wrapper; all dials fake.
        gw = self.spies.gateways[0]
        sink = ge.EventLog(log, self.err)
        self.addCleanup(sink.close)
        gw._event_log = sink
        for error, code in ((gsv.ProviderError("provider_redirect"), "provider_redirect"),
                            (gsv.ProviderError("provider_auth"), "provider_auth"),
                            (gsv.ProviderError("provider_protocol"), "provider_protocol"),
                            (RuntimeError(gf.KEY_CANARY), "provider_connect"),
                            (asyncio.CancelledError(), "provider_connect")):
            gw._deps.provider_connect = mock.AsyncMock(side_effect=error)
            with self.assertRaises(type(error)):
                await gw._connect()
            tail = [json.loads(line) for line in log.read_text().splitlines()][-2:]
            self.assertEqual([r["outcome"] for r in tail], ["attempt", "failed"])
            self.assertEqual(tail[-1]["code"], code)
        self.assertNotIn(gf.KEY_CANARY, log.read_text())


class LoggedFlowTest(flow.FlowCase):
    async def test_real_session_logs_outcome_not_transcript_reply_audio_or_credentials(self):
        path = self.base / "events"
        sink = ge.EventLog(path, io.StringIO())
        self.addCleanup(sink.close)
        self.client.reply_text = gf.REPLY_TEXT
        provider = flow.PcmProvider(self.clock, (flow.PCM_A,))
        fs = self.new_session(provider)
        cfg = srv.make_cfg(self.base, session_id=flow.SESSION_ID)
        deps = gsv.ServeDeps(
            provider_connect=mock.AsyncMock(return_value=provider),
            clock=lambda: self.clock,
        )
        gw = gsv._Gateway(
            cfg, gsv.Secret(gf.KEY_CANARY), gsv.AuthRegistry(), self.runner, self.ops, deps,
            ws_factory=srv.FakeServerWS, device_factory=lambda *_: fs.device,
            session_factory=lambda *_: fs.session, event_log=sink,
        )
        self.addAsyncCleanup(gw.shutdown)
        fs.session._provider_connect = gw._connect
        for request, status, code in (
            (srv.request_for(path="/" + gf.TEXT_CANARY), 404, "not_found"),
            (srv.request_for(bearer="Bearer " + gf.KEY_CANARY), 401, "unauthorized"),
            (srv.request_for(headers=[("Origin", gf.REPLY_TEXT)]), 400, "bad_request"),
        ):
            self.assertEqual((await gw.handle(request)).status, status)
            row = json.loads(path.read_text().splitlines()[-1])
            self.assertEqual((row["outcome"], row["status"], row["code"]),
                             ("refused", status, code))
        fs.task = asyncio.create_task(gw.handle(srv.request_for()))
        await self.quiesce()
        self.assertEqual((await gw.handle(srv.request_for())).status, 409)
        await fs.feed(gf.ASSISTANT_START)
        await fs.feed({"type": "pet.turn.begin"})
        turn = fs.device.of_type("pet.turn.began")[0]["turn_id"]
        await fs.feed(audio_frame(100))
        await fs.push(delegation())
        await fs.push(delta("e1", gf.TEXT_CANARY, 0, 500))
        await fs.feed(flow.golden_in(turn)["capture_stop"])
        await self.clock.advance(1.5)
        preview = fs.device.of_type("pet.turn.preview")[0]
        await fs.feed(self.confirm_frame(turn, sha=preview["text_sha256"]))
        await self.clock.advance(1)
        await self.clock.advance(3)
        await fs.feed({"type": "pet.stop"})
        await asyncio.wait_for(fs.task, 5)
        await until(lambda: not gw.busy)
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(self.client.last_text, gf.TEXT_CANARY)
        self.assertGreater(fs.session.outcome.audio_in_bytes, 0)
        self.assertGreater(fs.session.outcome.audio_out_bytes, 0)
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        connects = [r for r in rows if r["event"] == "device_connect"]
        self.assertEqual(len(connects), 5)  # three guard refusals, accepted, busy
        self.assertEqual(connects[3]["outcome"], "accepted")
        self.assertEqual(connects[3]["status"], 101)
        self.assertEqual(connects[-1]["code"], "busy")
        self.assertEqual([r["outcome"] for r in rows if r["event"] == "provider_dial"],
                         ["attempt", "open"])
        ends = [r for r in rows if r["event"] == "session_end"]
        self.assertEqual(len(ends), 1)
        end = dict(ends[0])
        end.pop("timestamp")
        end.pop("event")
        self.assertEqual(end, dataclasses.asdict(fs.session.outcome))
        for row in rows:
            fields = {
                "device_connect": {"outcome", "code", "status"},
                "provider_dial": {"outcome", "code"},
                "session_end": set(dataclasses.asdict(fs.session.outcome)),
            }[row["event"]]
            self.assertEqual(set(row), fields | {"timestamp", "event"})
        text = path.read_text()
        for canary in (gf.TEXT_CANARY, gf.REPLY_TEXT, gf.KEY_CANARY,
                       af.TOKEN, srv.GOOD_TOKEN, flow.SESSION_ID, af.HOST_IDENTITY,
                       flow.b64(flow.PCM_A), flow.b64(b"\x01\x02" * 100)):
            self.assertNotIn(canary, text)