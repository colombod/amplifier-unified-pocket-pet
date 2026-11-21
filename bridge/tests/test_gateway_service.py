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
from pet_bridge import gateway_state as gs
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
        if route.name == "sessions_observation":
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
        # The original service is stopped; give the isolated wrapper a live
        # synthetic runner and an affirmative local gate for this log-only test.
        gw._runner = gc.CoreRunner()
        gw._runner.start()
        self.addAsyncCleanup(gw._runner.aclose)
        gw._ops.pre_dial_admission = lambda: True
        gw._closing = False
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


class PreDialUncertaintyFlowTest(flow.FlowCase):
    """Real journal/core/custody with synthetic device, provider and Unified."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.devices = []
        self.providers = []
        self.dials = 0
        self.sockets = []

        async def connect(_key, *, timeout):
            self.assertEqual(timeout, gs.START_DEADLINE_S)
            self.dials += 1
            provider = gf.FakeProviderLink(self.clock)
            self.providers.append(provider)
            return provider

        def make_ws():
            ws = srv.FakeServerWS()
            self.sockets.append(ws)
            return ws

        cfg = srv.make_cfg(self.base, session_id=flow.SESSION_ID)
        self.gw = gsv._Gateway(
            cfg, gsv.Secret(gf.KEY_CANARY), gsv.AuthRegistry(), self.runner, self.ops,
            gsv.ServeDeps(provider_connect=connect, clock=lambda: self.clock),
            ws_factory=make_ws, device_factory=lambda *_: self.devices.pop(0),
        )
        self.addAsyncCleanup(self.gw.shutdown)

    async def test_gate_checks_frozen_session_ref_and_live_binding_locally(self):
        self.assertTrue(self.ops.pre_dial_safe())
        self.assertTrue(self.ops.pre_dial_admission())
        for field, value in (
            ("_binding", None),
            ("_session_id", "other-session"),
            ("_credential_ref", "device-" + "cd" * 16),
            ("_binding", dataclasses.replace(self.binding, binding_id=flow.IdFactory(3000)())),
        ):
            with self.subTest(field=field), mock.patch.object(self.ops, field, value):
                self.assertFalse(self.ops.pre_dial_safe())
                self.assertFalse(self.ops.pre_dial_admission())
        self.assertTrue(self.ops.pre_dial_safe())
        self.assertTrue(self.ops.pre_dial_admission())
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.dials, 0)

    async def test_lost_send_result_with_durable_uncertainty_does_not_redial(self):
        self.client.post_response = flow.Response(504, b"")
        gate = self.hold_posts()
        device = gf.FakeDeviceLink(self.clock)
        self.devices.append(device)
        task = asyncio.create_task(self.gw.handle(srv.request_for()))
        await self.quiesce()
        device.feed(gf.ASSISTANT_START)
        await self.quiesce()
        self.assertEqual(self.dials, 1)
        self.assertEqual(self.client.named("detail"), [])
        device.feed({"type": "pet.turn.begin"})
        await self.quiesce()
        turn = device.of_type("pet.turn.began")[0]["turn_id"]
        self.providers[0].push(delegation())
        self.providers[0].push(delta("e1", "hello world", 0, 500))
        await self.quiesce()
        device.feed(flow.golden_in(turn)["capture_stop"])
        await self.quiesce()
        await self.clock.advance(1.5)
        device.feed(self.confirm_frame(turn))
        await self.quiesce()
        await until(self.client.post_entered.is_set)
        self.assertTrue(self.client.post_blocked())
        device.fail_sends = True
        device.disconnect()
        await self.quiesce()
        await asyncio.wait_for(task, 5)
        self.assertEqual(device.of_type("pet.turn.send_result"), [])
        self.assertTrue(self.gw.busy)
        self.assertEqual((await self.gw.handle(srv.request_for())).status, 409)
        self.assertEqual(self.dials, 1)
        self.assertTrue(self.client.post_blocked())
        gate.set()
        await until(lambda: not self.gw.busy)
        self.assertEqual(self.record(turn)["state"], "UNCERTAIN")
        before = self.journal.get(f"cmd-{turn}.json")
        self.assertEqual(len(self.client.posts), 1)

        second = gf.FakeDeviceLink(self.clock)
        self.devices.append(second)
        second.feed(gf.ASSISTANT_START)
        retry = asyncio.create_task(self.gw.handle(srv.request_for()))
        await self.quiesce()
        if not retry.done():  # baseline lacks the fence; close its synthetic session
            second.feed({"type": "pet.stop"})
            await self.quiesce()
        await asyncio.wait_for(retry, 5)
        await until(lambda: not self.gw.busy)
        self.assertEqual(self.journal.get(f"cmd-{turn}.json"), before)
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(self.dials, 1)
        self.assertEqual(second.close_codes, [1008])
        self.assertEqual(second.of_type("session.started"), [])

        # Reopen the actual journal and core, not just a new session. Startup
        # recovery must preserve the unresolved command without another send.
        await self.gw.shutdown()
        await self.runner.aclose()
        self.journal.close()
        self.journal = flow.Journal(self.base / "journal")
        self.addCleanup(self.journal.close)
        self.bridge = core.Bridge(
            self.journal, flow.transport.HttpsTransport(self.store, lambda: self.client),
            clock=lambda: flow.WALL0 + self.clock.now(), new_id=flow.IdFactory(1000),
            monotonic=self.clock.now,
        )
        self.runner = gc.CoreRunner()
        self.runner.start()
        self.addAsyncCleanup(self.runner.aclose)
        self.ops = gc.UnifiedOps(
            self.bridge, self.store, flow.CRED_REF, flow.SESSION_ID, lambda: self.client,
            monotonic=self.clock.now, binding=self.binding,
        )
        old = self.gw
        self.gw = gsv._Gateway(
            old._cfg, gsv.Secret(gf.KEY_CANARY), gsv.AuthRegistry(), self.runner, self.ops,
            old._deps, ws_factory=old._ws_factory, device_factory=old._device_factory,
        )
        self.addAsyncCleanup(self.gw.shutdown)
        third = gf.FakeDeviceLink(self.clock)
        self.devices.append(third)
        third.feed(gf.ASSISTANT_START)
        restarted = asyncio.create_task(self.gw.handle(srv.request_for()))
        await self.quiesce()
        await asyncio.wait_for(restarted, 5)
        await until(lambda: not self.gw.busy)
        self.assertEqual(third.close_codes, [1008])
        self.assertEqual(self.journal.get(f"cmd-{turn}.json"), before)
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(self.dials, 1)
        self.client.echo_sent_text = True
        prior_reads = len(self.client.named("detail"))
        fourth = gf.FakeDeviceLink(self.clock)
        self.devices.append(fourth)
        fourth.feed(gf.ASSISTANT_START)
        recovered = asyncio.create_task(self.gw.handle(srv.request_for()))
        await self.quiesce()
        self.assertEqual(self.record(turn)["state"], "ACCEPTED")
        self.assertEqual(len(self.client.named("detail")) - prior_reads, 1)
        self.assertEqual(self.dials, 2)
        self.assertEqual(len(self.client.posts), 1)
        fourth.feed({"type": "pet.stop"})
        await self.quiesce()
        await asyncio.wait_for(recovered, 5)
        await until(lambda: not self.gw.busy)

    async def test_conversation_loses_asking_and_uncertainty_before_disconnect(self):
        self.client.post_response = flow.Response(504, b"")
        gate = self.hold_posts()
        device = gf.FakeDeviceLink(self.clock)
        self.devices.append(device)
        task = asyncio.create_task(self.gw.handle(srv.request_for()))
        await self.quiesce()
        device.feed(gf.ASSISTANT_START)
        await self.quiesce()
        device.feed({"type": "pet.converse.begin"})
        await self.quiesce()
        self.assertEqual(
            [f["state"] for f in device.of_type("pet.converse.state")], ["listening"]
        )
        self.providers[0].push(delta("e1", "hello world", 0, 500))
        self.providers[0].push(delegation())
        await self.quiesce()
        device.fail_sends = True
        await self.clock.advance(1.5)
        await until(self.client.post_entered.is_set)
        self.assertTrue(self.client.post_blocked())
        device.disconnect()
        await self.quiesce()
        await asyncio.wait_for(task, 5)
        self.assertNotIn(
            "asking", [f["state"] for f in device.of_type("pet.converse.state")]
        )
        self.assertEqual(device.of_type("pet.converse.notice"), [])
        self.assertTrue(self.gw.busy)
        self.assertEqual((await self.gw.handle(srv.request_for())).status, 409)
        # Make the retained-result lookup unavailable; the durable result remains
        # the source of truth and must still prevent a paid replacement session.
        with mock.patch.object(
            self.runner, "take_result", side_effect=gc.RunnerError("runner_result_unavailable")
        ):
            gate.set()
            await until(lambda: not self.gw.busy)
        self.assertEqual(self.gw.last_outcome.source, "unavailable")
        (name,) = self.command_names()
        before = self.journal.get(name)
        self.assertEqual(before["state"], "UNCERTAIN")

        second = gf.FakeDeviceLink(self.clock)
        self.devices.append(second)
        second.feed(gf.ASSISTANT_START)
        retry = asyncio.create_task(self.gw.handle(srv.request_for()))
        await self.quiesce()
        await asyncio.wait_for(retry, 5)
        await until(lambda: not self.gw.busy)
        self.assertEqual(second.close_codes, [1008])
        self.assertEqual(self.dials, 1)
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(self.journal.get(name), before)
        self.client.echo_sent_text = True
        real_connect = self.gw._deps.provider_connect
        observed = []

        async def checked_connect(*args, **kw):
            observed.append(self.journal.get(name)["state"])
            return await real_connect(*args, **kw)

        self.gw._deps.provider_connect = checked_connect
        third = gf.FakeDeviceLink(self.clock)
        self.devices.append(third)
        third.feed(gf.ASSISTANT_START)
        recovered = asyncio.create_task(self.gw.handle(srv.request_for()))
        await self.quiesce()
        self.assertEqual(observed, ["ACCEPTED"])
        self.assertEqual((self.dials, len(self.client.posts)), (2, 1))
        third.feed({"type": "pet.converse.begin"})
        await self.quiesce()
        self.assertEqual(
            [f["state"] for f in third.of_type("pet.converse.state")], ["listening"]
        )
        self.assertEqual(len(self.client.posts), 1)
        third.feed({"type": "pet.stop"})
        await self.quiesce()
        await asyncio.wait_for(recovered, 5)
        await until(lambda: not self.gw.busy)

    async def _cross_route_uncertainty_and_startup(self, first_tailnet, aged=False):
        self.gw._cfg = srv.make_cfg(
            self.base, session_id=flow.SESSION_ID,
            tailnet_bind_host="100.64.12.34", tailnet_fqdn="gateway.example.test",
        )
        lan = srv.request_for(
            transport=srv.transport_for(local=("10.1.2.3", 8941))
        )
        tail = srv.request_for(
            host="gateway.example.test:8941",
            transport=srv.transport_for(local=("100.64.12.34", 8941)),
        )
        first_route, other_route = (tail, lan) if first_tailnet else (lan, tail)
        self.client.post_response = flow.Response(504, b"")
        gate = self.hold_posts()
        device = gf.FakeDeviceLink(self.clock)
        self.devices.append(device)
        first = asyncio.create_task(self.gw.handle(first_route))
        await self.quiesce()
        device.feed(gf.ASSISTANT_START)
        await self.quiesce()
        self.assertEqual(self.dials, 1)
        device.feed({"type": "pet.turn.begin"})
        await self.quiesce()
        turn = device.of_type("pet.turn.began")[0]["turn_id"]
        self.providers[0].push(delta("e1", "hello world", 0, 500))
        self.providers[0].push(delegation())
        await self.quiesce()
        device.feed(flow.golden_in(turn)["capture_stop"])
        await self.quiesce()
        await self.clock.advance(1.5)
        device.feed(self.confirm_frame(turn))
        await until(self.client.post_entered.is_set)
        self.assertTrue(self.client.post_blocked())
        device.fail_sends = True
        device.disconnect()
        await self.quiesce()
        await asyncio.wait_for(first, 5)
        self.assertEqual(device.of_type("pet.turn.send_result"), [])
        self.assertTrue(self.gw.busy)
        self.assertEqual((await self.gw.handle(other_route)).status, 409)
        self.assertTrue(self.client.post_blocked())
        self.assertEqual((self.dials, len(self.client.posts)), (1, 1))
        gate.set()
        await until(lambda: not self.gw.busy)
        name = f"cmd-{turn}.json"
        before = self.journal.get(name)
        self.assertEqual(before["state"], "UNCERTAIN")

        second = gf.FakeDeviceLink(self.clock)
        self.devices.append(second)
        second.feed(gf.ASSISTANT_START)
        values = []
        original_gate = self.ops.pre_dial_admission

        def record_gate():
            value = original_gate()
            values.append(value)
            return value

        with mock.patch.object(self.ops, "pre_dial_admission", record_gate):
            retry = asyncio.create_task(self.gw.handle(other_route))
            await self.quiesce()
            if not retry.done():  # baseline without the fence needs a synthetic stop
                second.feed({"type": "pet.stop"})
                await self.quiesce()
            await asyncio.wait_for(retry, 5)
        await until(lambda: not self.gw.busy)
        self.assertEqual(values, [False])
        self.assertEqual(second.close_codes, [1008])
        self.assertEqual(second.of_type("session.started"), [])
        self.assertEqual(self.journal.get(name), before)
        self.assertEqual((self.dials, len(self.client.posts)), (1, 1))

        # Restart through the actual service startup with a fake site, client and
        # provider; reuse only this test's synthetic credential and journal.
        await self.gw.shutdown()
        await self.runner.aclose()
        self.journal.close()
        self.store.save_client_id(af.CLIENT_ID, flow.CRED_REF)
        self.store.close()
        fixture = srv.StateFixture(self.base)
        fixture.write("cert.pem", "synthetic cert")
        fixture.write("key.pem", "synthetic tls key")
        fixture.write("provider.key", gf.KEY_CANARY)
        cfg = srv.config_doc(
            self.base, state_root=str(self.base), session_id=flow.SESSION_ID,
            tailnet_bind_host="100.64.12.34", tailnet_fqdn="gateway.example.test",
        )
        config_path = fixture.write("config.json", json.dumps(cfg))
        spies = srv.Spies()
        client = self.client
        attaches = []

        class RestartUnified:
            def request(self, origin, route, *, headers=None, body=b""):
                if route.name == "attach":
                    attaches.append(True)
                    return af.json_response(af.attach_ok())
                return client.request(origin, route, headers=headers, body=body)

        spies.unified = RestartUnified()
        deps = spies.deps(self.clock)
        deps.provider_connect = self.gw._deps.provider_connect
        held = gsv._Held()
        self.addAsyncCleanup(gsv._release, held)
        startup_wall = flow.WALL0 + self.clock.now() + (core.RETENTION_S if aged else 0)
        with mock.patch.object(gsv.time, "time", return_value=startup_wall):
            await gsv._startup(held, config_path, {}, io.StringIO(), deps)
        restarted = held.gateway
        self.assertEqual(len(attaches), 1)
        after_startup = held.journal.get(name)
        if aged:
            self.assertEqual(after_startup["state"], "UNCERTAIN")
            self.assertTrue(after_startup["tombstone"])
            self.assertEqual(after_startup["binding"], before["binding"])
            self.assertEqual(after_startup["command_id"], turn)
            self.assertNotIn("text_sha256", after_startup)
        else:
            self.assertEqual(after_startup, before)
        self.assertEqual(len(spies.site_calls), 1)  # fake site, not a socket
        third = gf.FakeDeviceLink(self.clock)
        restarted._ws_factory = srv.FakeServerWS
        restarted._device_factory = lambda *_: third
        third.feed(gf.ASSISTANT_START)
        values = []
        original_gate = held.ops.pre_dial_admission
        with mock.patch.object(held.ops, "pre_dial_admission", record_gate):
            request = asyncio.create_task(restarted.handle(other_route))
            await asyncio.wait_for(request, 5)
        await until(lambda: not restarted.busy)
        self.assertEqual(len(attaches), 2)  # startup and session-boundary refresh
        self.assertEqual(values, [False])
        self.assertEqual(third.close_codes, [1008])
        self.assertEqual(third.of_type("session.started"), [])
        self.assertEqual(held.journal.get(name), after_startup)
        self.assertEqual((self.dials, len(self.client.posts)), (1, 1))
        if not aged:
            self.client.echo_sent_text = True
            observed = []
            real_connect = deps.provider_connect

            async def checked_connect(*args, **kw):
                observed.append(held.journal.get(name)["state"])
                return await real_connect(*args, **kw)

            deps.provider_connect = checked_connect
            fourth = gf.FakeDeviceLink(self.clock)
            restarted._device_factory = lambda *_: fourth
            fourth.feed(gf.ASSISTANT_START)
            values = []
            with mock.patch.object(held.ops._bridge, "_clock", return_value=startup_wall):
                with mock.patch.object(held.ops, "pre_dial_admission", record_gate):
                    recovered = asyncio.create_task(restarted.handle(other_route))
                    await until(lambda: bool(fourth.of_type("session.started")))
                    self.assertEqual(values, [True])
                    self.assertEqual(observed, ["ACCEPTED"])
                    self.assertEqual((self.dials, len(self.client.posts)), (2, 1))
                    fourth.feed({"type": "pet.stop"})
                    await asyncio.wait_for(recovered, 5)
                await until(lambda: not restarted.busy)
            self.assertEqual(held.journal.get(name)["state"], "ACCEPTED")
            self.assertEqual(len(attaches), 3)

    async def test_uncertainty_from_lan_refuses_tailnet_before_and_after_startup(self):
        await self._cross_route_uncertainty_and_startup(False)

    async def test_uncertainty_from_tailnet_refuses_lan_before_and_after_startup(self):
        await self._cross_route_uncertainty_and_startup(True)

    async def test_aged_lan_uncertainty_tombstone_refuses_tailnet_after_startup(self):
        await self._cross_route_uncertainty_and_startup(False, aged=True)

    async def test_aged_tailnet_uncertainty_tombstone_refuses_lan_after_startup(self):
        await self._cross_route_uncertainty_and_startup(True, aged=True)

    async def test_healthy_real_startup_allows_provider_without_a_send(self):
        fixture = srv.StateFixture(self.base)
        path = fixture.build(cfg={
            "tailnet_bind_host": "100.64.12.34",
            "tailnet_fqdn": "gateway.example.test",
        })
        spies = srv.Spies()
        spies.unified = RestartClient()
        deps = spies.deps(self.clock)
        deps.provider_connect = self.gw._deps.provider_connect
        held = gsv._Held()
        self.addAsyncCleanup(gsv._release, held)
        await gsv._startup(held, path, {}, io.StringIO(), deps)
        device = gf.FakeDeviceLink(self.clock)
        held.gateway._ws_factory = srv.FakeServerWS
        held.gateway._device_factory = lambda *_: device
        request = srv.request_for(
            host="gateway.example.test:8941",
            transport=srv.transport_for(local=("100.64.12.34", 8941)),
        )
        values = []
        original = held.ops.pre_dial_admission

        def record_gate():
            value = original()
            values.append(value)
            return value

        with mock.patch.object(held.ops, "pre_dial_admission", record_gate):
            task = asyncio.create_task(held.gateway.handle(request))
            device.feed(gf.ASSISTANT_START)
            await until(lambda: bool(device.of_type("session.started")))
            self.assertEqual(values, [True])
            self.assertEqual(self.dials, 1)
            device.feed({"type": "pet.stop"})
            await asyncio.wait_for(task, 5)
        await until(lambda: not held.gateway.busy)
        self.assertEqual(spies.unified.named("send"), [])

    async def _known_outcome_then_reconnect(self, response, expected):
        self.client.post_response = response
        gate = self.hold_posts()
        device = gf.FakeDeviceLink(self.clock)
        self.devices.append(device)
        first = asyncio.create_task(self.gw.handle(srv.request_for()))
        await self.quiesce()
        device.feed(gf.ASSISTANT_START)
        await self.quiesce()
        device.feed({"type": "pet.turn.begin"})
        await self.quiesce()
        turn = device.of_type("pet.turn.began")[0]["turn_id"]
        self.providers[0].push(delta("e1", "hello world", 0, 500))
        self.providers[0].push(delegation())
        await self.quiesce()
        device.feed(flow.golden_in(turn)["capture_stop"])
        await self.quiesce()
        await self.clock.advance(1.5)
        device.feed(self.confirm_frame(turn))
        await self.quiesce()
        await until(self.client.post_entered.is_set)
        self.assertTrue(self.client.post_blocked())
        device.fail_sends = True
        device.disconnect()
        await self.quiesce()
        await asyncio.wait_for(first, 5)
        self.assertTrue(self.gw.busy)
        gate.set()
        await until(lambda: not self.gw.busy)
        self.assertEqual(self.record(turn)["state"], expected)
        self.assertEqual(len(self.client.posts), 1)

        second = gf.FakeDeviceLink(self.clock)
        self.devices.append(second)
        second.feed(gf.ASSISTANT_START)
        retry = asyncio.create_task(self.gw.handle(srv.request_for()))
        await self.quiesce()
        self.assertEqual(self.dials, 2)
        self.assertEqual(len(second.of_type("session.started")), 1)
        second.feed({"type": "pet.stop"})
        await self.quiesce()
        await asyncio.wait_for(retry, 5)
        await until(lambda: not self.gw.busy)
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(self.record(turn)["state"], expected)

    async def test_lost_result_proven_accepted_allows_one_reconnect_without_resend(self):
        await self._known_outcome_then_reconnect(None, "ACCEPTED")

    async def test_lost_result_proven_rejected_allows_one_reconnect_without_resend(self):
        await self._known_outcome_then_reconnect(flow.Response(409, b"{}"), "REJECTED")

    async def test_exact_positive_proof_resolves_uncertain_before_reconnect(self):
        self.client.echo_sent_text = True
        await self._known_outcome_then_reconnect(flow.Response(504, b""), "ACCEPTED")

    async def _create_uncertain(self):
        # The first immediate read does not prove delivery; a later one does.
        self.client.post_response = flow.Response(504, b"")
        gate = self.hold_posts()
        device = gf.FakeDeviceLink(self.clock)
        self.devices.append(device)
        first = asyncio.create_task(self.gw.handle(srv.request_for()))
        await self.quiesce()
        device.feed(gf.ASSISTANT_START)
        await self.quiesce()
        device.feed({"type": "pet.turn.begin"})
        await self.quiesce()
        turn = device.of_type("pet.turn.began")[0]["turn_id"]
        self.providers[0].push(delta("e1", "hello world", 0, 500))
        self.providers[0].push(delegation())
        await self.quiesce()
        device.feed(flow.golden_in(turn)["capture_stop"])
        await self.quiesce()
        await self.clock.advance(1.5)
        device.feed(self.confirm_frame(turn))
        await until(self.client.post_entered.is_set)
        device.disconnect()
        await asyncio.wait_for(first, 5)
        self.assertTrue(self.gw.busy)
        gate.set()
        await until(lambda: not self.gw.busy)
        self.assertEqual(self.record(turn)["state"], "UNCERTAIN")
        self.assertEqual(self.dials, 1)
        return turn

    async def test_later_positive_proof_reconciles_before_replacement_dial(self):
        turn = await self._create_uncertain()
        prior_reads = len(self.client.named("detail"))
        self.client.echo_sent_text = True
        real_connect = self.gw._deps.provider_connect
        observed = []

        async def checked_connect(*args, **kw):
            observed.append((self.record(turn)["state"], len(self.client.named("detail"))))
            return await real_connect(*args, **kw)

        self.gw._deps.provider_connect = checked_connect

        second = gf.FakeDeviceLink(self.clock)
        self.devices.append(second)
        second.feed(gf.ASSISTANT_START)
        retry = asyncio.create_task(self.gw.handle(srv.request_for()))
        await self.quiesce()
        self.assertEqual(self.dials, 2)
        self.assertEqual(self.record(turn)["state"], "ACCEPTED")
        self.assertEqual(len(self.client.named("detail")) - prior_reads, 1)
        self.assertEqual(observed, [("ACCEPTED", prior_reads + 1)])
        self.assertEqual(len(self.client.posts), 1)
        second.feed({"type": "pet.stop"})
        await self.quiesce()
        await asyncio.wait_for(retry, 5)
        await until(lambda: not self.gw.busy)

    async def test_predial_proof_then_same_call_late_reply_recovers_without_replay(self):
        old = await self._create_uncertain()
        self.client.echo_sent_text = True
        self.client.post_response = None
        self.client.reply_pending = True
        old_answer = "OLD-LATE-REPLY-CANARY"
        new_answer = "NEW-DISTINCT-REPLY-CANARY"
        self.client.reply_text = old_answer
        device = gf.FakeDeviceLink(self.clock)
        self.devices.append(device)
        device.feed(gf.ASSISTANT_START)
        task = asyncio.create_task(self.gw.handle(srv.request_for()))
        await self.quiesce()
        self.assertEqual((self.dials, len(self.client.posts)), (2, 1))
        self.assertEqual(self.record(old)["state"], "ACCEPTED")
        device.feed({"type": "pet.converse.begin"})
        await self.quiesce()
        provider = self.providers[-1]
        provider.push(delta("e2", "request 2", 1000, 1500))
        provider.push(delegation("d2"))
        await self.quiesce()
        await self.clock.advance(1.5)
        timed_out = self.client.last_command
        self.assertNotEqual(old, timed_out)
        self.assertEqual(len(self.client.posts), 2)
        await self.clock.advance(180)
        self.assertEqual(device.of_type("pet.converse.notice")[-1]["code"], "reply_unavailable")
        provider.push_audio(gf.AUDIO_CANARY)
        await self.quiesce()
        for _ in range(16):
            provider.push_audio(bytes(4800))
            await self.quiesce()
            await self.clock.advance(0.1)
        self.client.reply_pending = False
        provider.push(delta("e3", "request 3", 2000, 2500))
        provider.push(delegation("d3"))
        await self.quiesce()
        await self.clock.advance(1.5)
        posts = [json.loads(p["body"]) for p in self.client.posts]
        self.assertEqual([p["args"]["text"] for p in posts],
                         ["hello world", "request 2", "request 3"])
        self.assertEqual(len({p["id"] for p in posts}), 3)
        self.assertEqual(self.record(timed_out)["reply_outcome"], "late_reply_not_spoken")
        self.assertEqual(self.dials, 2)  # same call: no extra provider dial
        self.assertNotIn(old_answer, [
            frame["content"] for frame in provider.of_type("session.commentary.append")
        ])
        self.client.reply_text = new_answer
        await self.clock.advance(1)
        self.assertEqual(
            [frame["content"] for frame in provider.of_type("session.commentary.append")],
            ["It's working on your request.", "No reply is available yet.", new_answer],
        )
        provider.push_audio(gf.AUDIO_CANARY)
        await self.quiesce()
        for _ in range(16):
            provider.push_audio(bytes(4800))
            await self.quiesce()
            await self.clock.advance(0.1)
        self.assertEqual(device.of_type("pet.converse.state")[-1]["state"], "listening")
        self.assertEqual(self.record(posts[-1]["id"])["state"], "COMPLETE")
        self.assertEqual((self.dials, len(self.client.posts)), (2, 3))
        device.feed({"type": "pet.stop"})
        await self.quiesce()
        await asyncio.wait_for(task, 5)
        await until(lambda: not self.gw.busy)

    async def test_cumulative_history_requires_exact_late_generation_before_next_turn(self):
        # The first send lost its acknowledgement and was not initially proven.
        first = await self._create_uncertain()
        self.assertEqual(self.record(first)["state"], "UNCERTAIN")
        self.client.post_response = None
        actual_request = self.client.request
        mode = "working"
        snapshots = []
        generations = ("gen-first", "gen-late", "gen-fresh")
        answers = ("first answer", "late answer", "fresh answer")

        def cumulative_detail(origin, route, *, headers=None, body=b""):
            if route.name != "detail":
                return actual_request(origin, route, headers=headers, body=body)
            posts = [json.loads(post["body"]) for post in self.client.posts]
            rows = []
            for index, post in enumerate(posts):
                generation = generations[index]
                user = flow.user_row(
                    post["id"], generation=generation, text=post["args"]["text"],
                    id=f"input-{index}",
                )
                answer_generation = generation
                if index == 1:
                    if mode == "working":
                        user["inputStatus"].update(
                            phase="working", basis="generation_started"
                        )
                        user["inputStatus"].pop("endedAt")
                    elif mode == "wrong_text":
                        user["text"] = "different input"
                    elif mode == "ambiguous_generation":
                        user["generationId"] = "gen-conflicting"
                    elif mode == "wrong_reply_generation":
                        answer_generation = "gen-other"
                rows.append(user)
                if index == 1 and mode == "decoy":
                    rows.append(flow.user_row(
                        "other-input", generation="gen-decoy", id="decoy",
                        nativeInputId=post["id"],
                    ))
                rows.append(flow.assistant_row(
                    answer_generation, answers[index], id=f"answer-{index}"
                ))
            if len(posts) == 3:
                rows.append(flow.assistant_row(
                    "gen-unrelated", "newer decoy answer", id="answer-decoy"
                ))
            snapshots.append(rows)
            return af.json_response(flow.page(rows))

        self.client.request = cumulative_detail
        device = gf.FakeDeviceLink(self.clock)
        self.devices.append(device)
        device.feed(gf.ASSISTANT_START)
        task = asyncio.create_task(self.gw.handle(srv.request_for()))
        await self.quiesce()
        self.assertEqual(self.record(first)["state"], "ACCEPTED")
        self.assertEqual(self.record(first)["reason"], "observed_in_conversation")
        self.assertEqual([row["inputId"] for row in snapshots[0] if row["role"] == "user"],
                         [first])
        self.assertEqual((self.dials, len(self.client.posts)), (2, 1))
        device.feed({"type": "pet.converse.begin"})
        await self.quiesce()
        provider = self.providers[-1]

        async def request(index):
            provider.push(delta(f"e{index}", f"request {index}", index * 1000, index * 1000 + 500))
            provider.push(delegation(f"d{index}"))
            await self.quiesce()
            await self.clock.advance(1.5)

        async def render_notice():
            provider.push_audio(gf.AUDIO_CANARY)
            await self.quiesce()
            for _ in range(16):
                provider.push_audio(bytes(4800))
                await self.quiesce()
                await self.clock.advance(0.1)

        await request(2)
        late = self.client.last_command
        self.assertNotEqual(first, late)
        self.assertEqual(self.record(first)["state"], "SUPPRESSED")
        self.assertEqual(self.record(late)["state"], "ACCEPTED")
        await self.clock.advance(180)
        self.assertEqual(device.of_type("pet.converse.notice")[-1]["code"], "reply_unavailable")
        await render_notice()

        # A completed older generation, wrong text, wrong answer generation,
        # ambiguous generation and native-input decoy do not settle the late input.
        for index, mode in enumerate(
            ("working", "wrong_text", "wrong_reply_generation", "ambiguous_generation", "decoy"), 3
        ):
            with self.subTest(mode=mode):
                reads = len(snapshots)
                notice_count = len(device.of_type("pet.converse.notice"))
                await request(index)
                self.assertGreater(len(snapshots), reads)
                rows = snapshots[-1]
                self.assertEqual(rows[0]["inputId"], first)
                self.assertEqual(rows[1]["generationId"], generations[0])
                self.assertEqual(rows[2]["inputId"], late)
                self.assertEqual(rows[2]["inputStatus"]["generationId"], generations[1])
                if mode == "wrong_text":
                    self.assertEqual(rows[2]["text"], "different input")
                elif mode == "wrong_reply_generation":
                    self.assertEqual(rows[3]["generationId"], "gen-other")
                elif mode == "ambiguous_generation":
                    self.assertEqual(rows[2]["generationId"], "gen-conflicting")
                elif mode == "decoy":
                    self.assertEqual(rows[3]["nativeInputId"], late)
                self.assertEqual(len(self.client.posts), 2)
                self.assertEqual(self.record(late)["state"], "ACCEPTED")
                self.assertFalse(self.record(late).get("reply_consumed", False))
                self.assertEqual(len(device.of_type("pet.converse.notice")), notice_count + 1)
                self.assertEqual(device.of_type("pet.converse.notice")[-1]["code"], "busy")
                await render_notice()
                self.assertEqual(device.of_type("pet.converse.state")[-1]["state"], "listening")

        mode = "complete"
        await request(8)
        posts = [json.loads(post["body"]) for post in self.client.posts]
        self.assertEqual([post["args"]["text"] for post in posts],
                         ["hello world", "request 2", "request 8"])
        self.assertEqual(len({post["id"] for post in posts}), 3)
        self.assertEqual(self.record(late)["reply_outcome"], "late_reply_not_spoken")
        self.assertEqual(self.dials, 2)
        await self.clock.advance(1)
        rows = snapshots[-1]
        self.assertEqual([row["inputId"] for row in rows if row["role"] == "user"],
                         [post["id"] for post in posts])
        self.assertEqual([row["generationId"] for row in rows if row["role"] == "assistant"],
                         [*generations, "gen-unrelated"])
        self.assertEqual([row["text"] for row in rows if row["role"] == "assistant"],
                         [*answers, "newer decoy answer"])
        commentary = [frame["content"] for frame in provider.of_type("session.commentary.append")]
        for forbidden in (answers[0], answers[1], "newer decoy answer"):
            self.assertFalse(any(forbidden in frame for frame in commentary))
        self.assertEqual(commentary.count(answers[2]), 1)
        prior_audio = len(device.of_type("session.output_audio.delta"))
        await render_notice()
        self.assertGreater(len(device.of_type("session.output_audio.delta")), prior_audio)
        self.assertEqual(device.of_type("pet.converse.state")[-1]["state"], "listening")
        self.assertEqual(self.record(posts[2]["id"])["state"], "COMPLETE")
        self.assertEqual((self.dials, len(self.client.posts)), (2, 3))
        device.feed({"type": "pet.stop"})
        await self.quiesce()
        await asyncio.wait_for(task, 5)
        await until(lambda: not self.gw.busy)
        self.assertEqual(self.gw.last_outcome.conversation.replies_rendered, 1)
        commentary = [frame["content"] for frame in provider.of_type("session.commentary.append")]
        # One fixed hint preceded this timeout; no late/decoy answer was rendered.
        self.assertEqual(commentary, ["It's working on your request.", "No reply is available yet."] +
                         ["That conversation is busy."] * 5 + [answers[2]])

    async def test_late_reply_proof_disconnect_retains_custody_until_read_finishes(self):
        self.client.echo_sent_text = True
        self.client.reply_pending = True
        device = gf.FakeDeviceLink(self.clock)
        self.devices.append(device)
        device.feed(gf.ASSISTANT_START)
        task = asyncio.create_task(self.gw.handle(srv.request_for()))
        await self.quiesce()
        device.feed({"type": "pet.converse.begin"})
        await self.quiesce()
        provider = self.providers[-1]
        provider.push(delta("e1", "request 1", 1000, 1500))
        provider.push(delegation("d1"))
        await self.quiesce()
        await self.clock.advance(1.5)
        old = self.client.last_command
        await self.clock.advance(180)
        provider.push_audio(gf.AUDIO_CANARY)
        await self.quiesce()
        for _ in range(16):
            provider.push_audio(bytes(4800))
            await self.quiesce()
            await self.clock.advance(0.1)
        self.client.reply_pending = False
        gate = self.hold_posts()
        entered = threading.Event()
        actual = self.client.request
        def held_request(origin, route, **kwargs):
            if route.name == "detail":
                entered.set()
                if not gate.wait(10):
                    raise AssertionError("synthetic proof gate timed out")
            return actual(origin, route, **kwargs)
        self.client.request = held_request
        provider.push(delta("e2", "request 2", 2000, 2500))
        provider.push(delegation("d2"))
        await self.quiesce()
        await self.clock.advance(1.5)
        await until(entered.is_set)
        device.disconnect()
        await self.quiesce()
        await asyncio.wait_for(task, 5)
        self.assertTrue(self.gw.busy)
        self.assertEqual((await self.gw.handle(srv.request_for())).status, 409)
        self.assertEqual((self.dials, len(self.client.posts)), (1, 1))
        gate.set()
        await until(lambda: not self.gw.busy)
        self.assertEqual(self.record(old)["state"], "ACCEPTED")
        self.assertFalse(self.record(old).get("reply_consumed", False))
        self.assertEqual((self.dials, len(self.client.posts)), (1, 1))
        self.assertNotIn(self.client.reply_text, [
            frame["content"] for frame in provider.of_type("session.commentary.append")
        ])

    async def test_complete_older_page_proof_is_promoted_before_dial(self):
        turn = await self._create_uncertain()
        rows = [flow.user_row(turn, text=self.client.last_text)]
        rows.extend(flow.assistant_row(None, "older", id=f"hist-{i}") for i in range(129))
        server = tc.DetailServer(rows, limit=60)
        real_request = self.client.request

        def request(origin, route, *, headers=None, body=b""):
            if route.name == "detail":
                return server({"route": route})
            return real_request(origin, route, headers=headers, body=body)

        self.client.request = request
        real_connect = self.gw._deps.provider_connect
        observed = []

        async def checked_connect(*args, **kw):
            observed.append((self.record(turn)["state"], len(server.requests)))
            return await real_connect(*args, **kw)

        self.gw._deps.provider_connect = checked_connect
        device = gf.FakeDeviceLink(self.clock)
        self.devices.append(device)
        device.feed(gf.ASSISTANT_START)
        task = asyncio.create_task(self.gw.handle(srv.request_for()))
        await self.quiesce()
        self.assertEqual(observed, [("ACCEPTED", 3)])
        self.assertEqual(len(server.requests), 3)
        self.assertEqual(len(device.of_type("session.started")), 1)
        self.assertEqual((self.dials, len(self.client.posts)), (2, 1))
        device.feed({"type": "pet.stop"})
        await self.quiesce()
        await asyncio.wait_for(task, 5)
        await until(lambda: not self.gw.busy)

    async def test_native_decoy_on_other_page_never_promotes(self):
        turn = await self._create_uncertain()
        before = self.record(turn)
        proper = flow.user_row(turn, text=self.client.last_text, id="proof")
        decoy = flow.user_row("other", id="decoy", nativeInputId=turn)
        fillers = [flow.assistant_row(None, "history", id=f"hist-{i}") for i in range(120)]
        real_request = self.client.request
        for proof_first in (True, False):
            with self.subTest(proof_first=proof_first):
                rows = ([proper, *fillers, decoy] if proof_first
                        else [decoy, *fillers, proper])
                server = tc.DetailServer(rows, limit=60)

                def request(origin, route, *, headers=None, body=b""):
                    if route.name == "detail":
                        return server({"route": route})
                    return real_request(origin, route, headers=headers, body=body)

                self.client.request = request
                device = gf.FakeDeviceLink(self.clock)
                self.devices.append(device)
                device.feed(gf.ASSISTANT_START)
                task = asyncio.create_task(self.gw.handle(srv.request_for()))
                await self.quiesce()
                await asyncio.wait_for(task, 5)
                await until(lambda: not self.gw.busy)
                self.assertEqual(len(server.requests), 3)
                self.assertEqual(device.close_codes, [1008])
                self.assertEqual(self.record(turn), before)
                self.assertEqual((self.dials, len(self.client.posts)), (1, 1))
        self.client.request = real_request

    async def test_admission_bad_wall_clock_refuses_before_and_after_held_read(self):
        turn = await self._create_uncertain()
        before = self.record(turn)
        self.client.echo_sent_text = True
        real_request = self.client.request
        for bad in (float("nan"), float("inf"), True, None, "bad",
                    before["updated_at"] - 1, before["updated_at"] + core.RETENTION_S):
            with self.subTest(phase="before", value=repr(bad)):
                old_reads = len(self.client.named("detail"))
                device = gf.FakeDeviceLink(self.clock)
                self.devices.append(device)
                device.feed(gf.ASSISTANT_START)
                with mock.patch.object(self.bridge, "_clock", return_value=bad):
                    task = asyncio.create_task(self.gw.handle(srv.request_for()))
                    await self.quiesce()
                    await asyncio.wait_for(task, 5)
                    await until(lambda: not self.gw.busy)
                self.assertEqual(len(self.client.named("detail")), old_reads)
                self.assertEqual(device.close_codes, [1008])
                self.assertEqual(self.record(turn), before)
                self.assertEqual((self.dials, len(self.client.posts)), (1, 1))

        for bad in (float("nan"), True, before["updated_at"] - 1,
                    before["updated_at"] + core.RETENTION_S):
            with self.subTest(phase="after", value=repr(bad)):
                entered, release = threading.Event(), threading.Event()
                self.addCleanup(release.set)

                def request(origin, route, *, headers=None, body=b""):
                    if route.name == "detail":
                        entered.set()
                        if not release.wait(10):
                            raise AssertionError("detail hold timed out")
                    return real_request(origin, route, headers=headers, body=body)

                self.client.request = request
                device = gf.FakeDeviceLink(self.clock)
                self.devices.append(device)
                device.feed(gf.ASSISTANT_START)
                task = asyncio.create_task(self.gw.handle(srv.request_for()))
                await until(entered.is_set)
                with mock.patch.object(self.bridge, "_clock", return_value=bad):
                    release.set()
                    await asyncio.wait_for(task, 5)
                    await until(lambda: not self.gw.busy)
                self.assertEqual(device.close_codes, [1008])
                self.assertEqual(self.record(turn), before)
                self.assertEqual((self.dials, len(self.client.posts)), (1, 1))
                self.client.request = real_request

    async def test_no_proof_bad_digest_foreign_ambiguous_and_read_errors_refuse(self):
        turn = await self._create_uncertain()
        original = self.record(turn)
        proper = flow.user_row(turn, text=self.client.last_text)
        cases = {
            "absent": [flow.user_row("other", text=self.client.last_text)],
            "wrong_text": [flow.user_row(turn, text="wrong")],
            "truncated": [{**proper, "textDetail": {"truncated": True}}],
            "foreign": [{**proper, "sessionId": "different-session"}],
            "native_decoy": [proper, flow.user_row("other", id="decoy", nativeInputId=turn)],
            "ambiguous": [{**proper, "nativeInputAmbiguous": True}],
            "duplicate": [proper, {**proper, "id": "other-row"}],
            "read_error": None,
        }
        real_request = self.client.request
        for name, rows in cases.items():
            with self.subTest(case=name):
                def request(origin, route, *, headers=None, body=b""):
                    if route.name == "detail":
                        if rows is None:
                            raise OSError(gf.KEY_CANARY)
                        return flow.json_response(flow.page(rows))
                    return real_request(origin, route, headers=headers, body=body)

                self.client.request = request
                device = gf.FakeDeviceLink(self.clock)
                self.devices.append(device)
                device.feed(gf.ASSISTANT_START)
                task = asyncio.create_task(self.gw.handle(srv.request_for()))
                await self.quiesce()
                await asyncio.wait_for(task, 5)
                await until(lambda: not self.gw.busy)
                self.assertEqual(device.close_codes, [1008])
                self.assertEqual(self.record(turn), original)
                self.assertEqual(self.dials, 1)
                self.assertEqual(len(self.client.posts), 1)
                self.assertNotIn(gf.KEY_CANARY, repr(device.frames))
        self.client.request = real_request

    async def test_incomplete_history_with_visible_proof_stays_uncertain(self):
        turn = await self._create_uncertain()
        before = self.record(turn)
        self.client.echo_sent_text = True
        self.client.history_rows = [
            flow.assistant_row(None, "older", id=f"history-{i}") for i in range(639)
        ]
        old_reads = len(self.client.named("detail"))
        device = gf.FakeDeviceLink(self.clock)
        self.devices.append(device)
        device.feed(gf.ASSISTANT_START)
        task = asyncio.create_task(self.gw.handle(srv.request_for()))
        await self.quiesce()
        await asyncio.wait_for(task, 5)
        await until(lambda: not self.gw.busy)
        self.assertEqual(len(self.client.named("detail")) - old_reads, 4)
        self.assertEqual(self.record(turn), before)
        self.assertEqual(device.close_codes, [1008])
        self.assertEqual(self.dials, 1)
        self.assertEqual(len(self.client.posts), 1)

    async def test_foreign_envelope_cannot_promote_exact_matching_input(self):
        turn = await self._create_uncertain()
        before = self.record(turn)
        real_request = self.client.request

        def request(origin, route, *, headers=None, body=b""):
            if route.name == "detail":
                row = flow.user_row(turn, text=self.client.last_text)
                return flow.json_response({**flow.page([row]), "sessionId": "foreign-session"})
            return real_request(origin, route, headers=headers, body=body)

        self.client.request = request
        device = gf.FakeDeviceLink(self.clock)
        self.devices.append(device)
        device.feed(gf.ASSISTANT_START)
        task = asyncio.create_task(self.gw.handle(srv.request_for()))
        await self.quiesce()
        await asyncio.wait_for(task, 5)
        await until(lambda: not self.gw.busy)
        self.assertEqual(self.record(turn), before)
        self.assertEqual(device.close_codes, [1008])
        self.assertEqual(self.dials, 1)
        self.assertEqual(len(self.client.posts), 1)

    async def test_expiry_during_detail_read_keeps_uncertainty_and_never_dials(self):
        turn = await self._create_uncertain()
        before = self.record(turn)
        self.client.echo_sent_text = True
        real_request = self.client.request
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def request(origin, route, *, headers=None, body=b""):
            if route.name == "detail":
                entered.set()
                if not release.wait(10):
                    raise AssertionError("detail hold timed out")
            return real_request(origin, route, headers=headers, body=body)

        self.client.request = request
        device = gf.FakeDeviceLink(self.clock)
        self.devices.append(device)
        device.feed(gf.ASSISTANT_START)
        task = asyncio.create_task(self.gw.handle(srv.request_for()))
        await until(entered.is_set)
        with mock.patch.object(
            self.bridge, "_clock", return_value=before["updated_at"] + core.RETENTION_S
        ):
            release.set()
            await asyncio.wait_for(task, 5)
            await until(lambda: not self.gw.busy)
        self.assertEqual(self.record(turn), before)
        self.assertEqual(device.close_codes, [1008])
        self.assertEqual(self.dials, 1)
        self.assertEqual(len(self.client.posts), 1)

    async def test_later_page_failure_does_not_promote_visible_newest_proof(self):
        turn = await self._create_uncertain()
        before = self.record(turn)
        self.client.echo_sent_text = True
        self.client.history_rows = [
            flow.assistant_row(None, "older", id=f"history-{i}") for i in range(70)
        ]
        real_request = self.client.request
        calls = 0

        def request(origin, route, *, headers=None, body=b""):
            nonlocal calls
            if route.name == "detail":
                calls += 1
                if calls == 2:
                    return flow.Response(503, b"")
            return real_request(origin, route, headers=headers, body=body)

        self.client.request = request
        device = gf.FakeDeviceLink(self.clock)
        self.devices.append(device)
        device.feed(gf.ASSISTANT_START)
        task = asyncio.create_task(self.gw.handle(srv.request_for()))
        await self.quiesce()
        await asyncio.wait_for(task, 5)
        await until(lambda: not self.gw.busy)
        self.assertEqual(calls, 2)
        self.assertEqual(self.record(turn), before)
        self.assertEqual(device.close_codes, [1008])
        self.assertEqual(self.dials, 1)
        self.assertEqual(len(self.client.posts), 1)

    async def test_new_blocker_during_detail_read_prevents_promotion_and_dial(self):
        turn = await self._create_uncertain()
        before = self.record(turn)
        self.client.echo_sent_text = True
        real_request = self.client.request
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def request(origin, route, *, headers=None, body=b""):
            if route.name == "detail":
                entered.set()
                if not release.wait(10):
                    raise AssertionError("detail hold timed out")
            return real_request(origin, route, headers=headers, body=body)

        self.client.request = request
        device = gf.FakeDeviceLink(self.clock)
        self.devices.append(device)
        device.feed(gf.ASSISTANT_START)
        task = asyncio.create_task(self.gw.handle(srv.request_for()))
        await until(entered.is_set)
        other = "00000000-0000-4000-8000-0000000000b3"
        self.journal.put(f"cmd-{other}.json", {**before, "state": "ATTEMPTED"})
        release.set()
        await asyncio.wait_for(task, 5)
        await until(lambda: not self.gw.busy)
        self.assertEqual(self.record(turn), before)
        self.assertEqual(device.close_codes, [1008])
        self.assertEqual(self.dials, 1)
        self.assertEqual(len(self.client.posts), 1)

    async def test_reselected_binding_during_detail_read_prevents_promotion(self):
        turn = await self._create_uncertain()
        before = self.record(turn)
        self.client.echo_sent_text = True
        real_request = self.client.request
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def request(origin, route, *, headers=None, body=b""):
            if route.name == "detail":
                entered.set()
                if not release.wait(10):
                    raise AssertionError("detail hold timed out")
            return real_request(origin, route, headers=headers, body=body)

        self.client.request = request
        device = gf.FakeDeviceLink(self.clock)
        self.devices.append(device)
        device.feed(gf.ASSISTANT_START)
        task = asyncio.create_task(self.gw.handle(srv.request_for()))
        await until(entered.is_set)
        new_binding = self.bridge.select_session(
            host_id=self.binding.host_id,
            host_instance_id=self.binding.host_instance_id,
            session_id=self.binding.session_id,
            client_id=self.binding.client_id,
            credential_ref=self.binding.credential_ref,
        )
        self.assertNotEqual(new_binding.binding_id, self.binding.binding_id)
        release.set()
        await asyncio.wait_for(task, 5)
        await until(lambda: not self.gw.busy)
        self.assertEqual(self.record(turn), before)
        self.assertEqual(device.close_codes, [1008])
        self.assertEqual((self.dials, len(self.client.posts)), (1, 1))

    async def test_unreconcilable_tombstone_never_reads_or_dials(self):
        turn = await self._create_uncertain()
        record = self.record(turn)
        tomb = {key: record[key] for key in (
            "state", "binding", "epoch", "created_at", "updated_at", "reason"
        )}
        tomb.update(command_id=turn, event_ids=[], tombstone=True)
        self.journal.put(f"cmd-{turn}.json", tomb)
        before = self.record(turn)
        self.client.echo_sent_text = True
        old_reads = len(self.client.named("detail"))
        device = gf.FakeDeviceLink(self.clock)
        self.devices.append(device)
        device.feed(gf.ASSISTANT_START)
        task = asyncio.create_task(self.gw.handle(srv.request_for()))
        await self.quiesce()
        await asyncio.wait_for(task, 5)
        await until(lambda: not self.gw.busy)
        self.assertEqual(len(self.client.named("detail")), old_reads)
        self.assertEqual(self.record(turn), before)
        self.assertEqual(device.close_codes, [1008])
        self.assertEqual(self.dials, 1)
        self.assertEqual(len(self.client.posts), 1)

    async def test_other_hard_blocker_prevents_detail_read_even_with_uncertainty(self):
        turn = await self._create_uncertain()
        record = self.record(turn)
        second = "00000000-0000-4000-8000-0000000000b3"
        self.journal.put(f"cmd-{second}.json", {**record, "state": "ATTEMPTED"})
        old_reads = len(self.client.named("detail"))
        self.client.echo_sent_text = True
        device = gf.FakeDeviceLink(self.clock)
        self.devices.append(device)
        device.feed(gf.ASSISTANT_START)
        task = asyncio.create_task(self.gw.handle(srv.request_for()))
        await self.quiesce()
        await asyncio.wait_for(task, 5)
        await until(lambda: not self.gw.busy)
        self.assertEqual(len(self.client.named("detail")), old_reads)
        self.assertEqual(self.record(turn), record)
        self.assertEqual(device.close_codes, [1008])
        self.assertEqual(self.dials, 1)
        self.assertEqual(len(self.client.posts), 1)

    async def test_partial_multi_command_proof_does_not_bypass_remaining_uncertainty(self):
        turn = await self._create_uncertain()
        first = self.record(turn)
        other = "00000000-0000-4000-8000-0000000000b3"
        self.journal.put(f"cmd-{other}.json", {**first, "event_ids": ["other-event"]})
        self.client.echo_sent_text = True
        prior_reads = len(self.client.named("detail"))
        device = gf.FakeDeviceLink(self.clock)
        self.devices.append(device)
        device.feed(gf.ASSISTANT_START)
        task = asyncio.create_task(self.gw.handle(srv.request_for()))
        await self.quiesce()
        await asyncio.wait_for(task, 5)
        await until(lambda: not self.gw.busy)
        self.assertEqual(len(self.client.named("detail")) - prior_reads, 1)
        self.assertEqual(self.record(turn)["state"], "ACCEPTED")
        self.assertEqual(self.record(other)["state"], "UNCERTAIN")
        self.assertEqual(device.close_codes, [1008])
        self.assertEqual(self.dials, 1)
        self.assertEqual(len(self.client.posts), 1)

    async def test_failed_proof_persistence_faults_and_refuses_dial(self):
        turn = await self._create_uncertain()
        self.client.echo_sent_text = True
        actual_put = self.journal.put

        def put(name, record):
            if name == f"cmd-{turn}.json" and record.get("state") == "ACCEPTED":
                raise flow.core.JournalError("journal_io_error")
            return actual_put(name, record)

        device = gf.FakeDeviceLink(self.clock)
        self.devices.append(device)
        device.feed(gf.ASSISTANT_START)
        with mock.patch.object(self.journal, "put", side_effect=put):
            task = asyncio.create_task(self.gw.handle(srv.request_for()))
            await self.quiesce()
            await asyncio.wait_for(task, 5)
            await until(lambda: not self.gw.busy)
        self.assertTrue(self.bridge.status()["faulted"])
        self.assertEqual(self.record(turn)["state"], "UNCERTAIN")
        self.assertEqual(device.close_codes, [1008])
        self.assertEqual(self.dials, 1)
        self.assertEqual(len(self.client.posts), 1)

    async def test_unknown_persistence_after_partial_promotion_faults_and_refuses(self):
        turn = await self._create_uncertain()
        first = self.record(turn)
        other = "00000000-0000-4000-8000-0000000000b3"
        self.journal.put(f"cmd-{other}.json", {**first, "event_ids": ["other-event"]})
        real_request = self.client.request

        def request(origin, route, *, headers=None, body=b""):
            if route.name == "detail":
                return flow.json_response(flow.page([
                    flow.user_row(turn, text=self.client.last_text),
                    flow.user_row(other, text=self.client.last_text),
                ]))
            return real_request(origin, route, headers=headers, body=body)

        self.client.request = request
        actual_put = self.journal.put
        writes = []

        def put(name, record):
            if name.startswith("cmd-") and record.get("state") == "ACCEPTED":
                writes.append(name)
                actual_put(name, record)
                if len(writes) == 2:
                    raise flow.core.JournalError("journal_persist_unknown")
                return
            return actual_put(name, record)

        device = gf.FakeDeviceLink(self.clock)
        self.devices.append(device)
        device.feed(gf.ASSISTANT_START)
        with mock.patch.object(self.journal, "put", side_effect=put):
            task = asyncio.create_task(self.gw.handle(srv.request_for()))
            await self.quiesce()
            await asyncio.wait_for(task, 5)
            await until(lambda: not self.gw.busy)
        self.assertEqual(len(writes), 2)
        self.assertTrue(self.bridge.status()["faulted"])
        self.assertEqual(device.close_codes, [1008])
        self.assertEqual(self.dials, 1)
        self.assertEqual(len(self.client.posts), 1)

    async def _cancel_during_held_detail(self, how):
        turn = await self._create_uncertain()
        self.client.echo_sent_text = True
        real_request = self.client.request
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def held_request(origin, route, *, headers=None, body=b""):
            if route.name == "detail":
                entered.set()
                if not release.wait(10):
                    raise AssertionError("detail hold timed out")
            return real_request(origin, route, headers=headers, body=body)

        self.client.request = held_request
        device = gf.FakeDeviceLink(self.clock)
        self.devices.append(device)
        device.feed(gf.ASSISTANT_START)
        task = asyncio.create_task(self.gw.handle(srv.request_for()))
        await until(entered.is_set)
        self.assertEqual((await self.gw.handle(srv.request_for())).status, 409)
        shutdown = None
        if how == "stop":
            device.feed({"type": "pet.stop"})
        elif how == "cancel":
            task.cancel()
        elif how == "disconnect":
            device.disconnect()
        else:
            shutdown = asyncio.create_task(self.gw.shutdown())
        await until(lambda: task.done() or (shutdown is not None and self.gw.closing))
        self.assertTrue(self.gw.busy)
        self.assertEqual(self.dials, 1)
        self.assertEqual(self.record(turn)["state"], "UNCERTAIN")
        release.set()
        if how == "cancel":
            with self.assertRaises(asyncio.CancelledError):
                await task
        else:
            await asyncio.wait_for(task, 5)
        if shutdown is not None:
            await asyncio.wait_for(shutdown, 5)
        await until(lambda: not self.gw.busy)
        # The synchronous read may still commit exact proof after Stop; it must
        # never restart the cancelled dial or resend the original input.
        self.assertEqual(self.record(turn)["state"], "ACCEPTED")
        self.assertEqual(self.dials, 1)
        self.assertEqual(len(self.client.posts), 1)

    async def test_stop_while_detail_read_held_never_dials_late(self):
        await self._cancel_during_held_detail("stop")

    async def test_http_cancel_while_detail_read_held_never_dials_late(self):
        await self._cancel_during_held_detail("cancel")

    async def test_shutdown_while_detail_read_held_never_dials_late(self):
        await self._cancel_during_held_detail("shutdown")

    async def test_disconnect_while_detail_read_held_never_dials_late(self):
        await self._cancel_during_held_detail("disconnect")

    async def test_start_deadline_while_detail_held_never_dials_late(self):
        turn = await self._create_uncertain()
        self.client.echo_sent_text = True
        real_request = self.client.request
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def held_request(origin, route, *, headers=None, body=b""):
            if route.name == "detail":
                entered.set()
                if not release.wait(10):
                    raise AssertionError("detail hold timed out")
            return real_request(origin, route, headers=headers, body=body)

        self.client.request = held_request
        device = gf.FakeDeviceLink(self.clock)
        self.devices.append(device)
        device.feed(gf.ASSISTANT_START)
        task = asyncio.create_task(self.gw.handle(srv.request_for()))
        await until(entered.is_set)
        old_hook, self.clock.settle_hook = self.clock.settle_hook, None
        try:
            await self.clock.advance(gs.START_DEADLINE_S)
        finally:
            self.clock.settle_hook = old_hook
        await asyncio.wait_for(task, 5)
        self.assertTrue(self.gw.busy)
        self.assertEqual(self.dials, 1)
        release.set()
        await until(lambda: not self.gw.busy)
        self.assertEqual(self.record(turn)["state"], "ACCEPTED")
        self.assertEqual(self.dials, 1)
        self.assertEqual(len(self.client.posts), 1)

    async def test_corrupt_journal_refuses_before_any_counted_provider_dial(self):
        self.journal.put("binding.json", {"binding": None, "epoch": True, "stopped": 0})
        before = (self.base / "journal" / "binding.json").read_bytes()
        device = gf.FakeDeviceLink(self.clock)
        self.devices.append(device)
        device.feed(gf.ASSISTANT_START)
        task = asyncio.create_task(self.gw.handle(srv.request_for()))
        await self.quiesce()
        await asyncio.wait_for(task, 5)
        await until(lambda: not self.gw.busy)
        self.assertEqual(device.close_codes, [1008])
        self.assertEqual(self.dials, 0)
        self.assertEqual(self.client.posts, [])
        self.assertEqual((self.base / "journal" / "binding.json").read_bytes(), before)

    async def test_proven_presend_refusal_cleans_turn_and_allows_reconnect(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def refuse_bad_digest(turn, _sha, _revision):
            entered.set()
            if not release.wait(10):
                raise AssertionError("fake confirm hold timed out")
            return self.bridge.confirm_send(turn, expected_text_sha256="0" * 64)

        self.ops.confirm = refuse_bad_digest
        device = gf.FakeDeviceLink(self.clock)
        self.devices.append(device)
        first = asyncio.create_task(self.gw.handle(srv.request_for()))
        await self.quiesce()
        device.feed(gf.ASSISTANT_START)
        await self.quiesce()
        device.feed({"type": "pet.turn.begin"})
        await self.quiesce()
        turn = device.of_type("pet.turn.began")[0]["turn_id"]
        self.providers[0].push(delta("e1", "hello world", 0, 500))
        self.providers[0].push(delegation())
        await self.quiesce()
        device.feed(flow.golden_in(turn)["capture_stop"])
        await self.quiesce()
        await self.clock.advance(1.5)
        device.feed(self.confirm_frame(turn))
        await until(entered.is_set)
        device.fail_sends = True
        device.disconnect()
        await asyncio.wait_for(first, 5)
        self.assertTrue(self.gw.busy)
        release.set()
        await until(lambda: not self.gw.busy)
        self.assertEqual(self.gw.last_outcome.send_outcome, "refused")
        self.assertEqual(self.command_names(), [])
        self.assertEqual(self.client.posts, [])

        second = gf.FakeDeviceLink(self.clock)
        self.devices.append(second)
        second.feed(gf.ASSISTANT_START)
        retry = asyncio.create_task(self.gw.handle(srv.request_for()))
        await self.quiesce()
        self.assertEqual(self.dials, 2)
        self.assertEqual(len(second.of_type("session.started")), 1)
        second.feed({"type": "pet.stop"})
        await self.quiesce()
        await asyncio.wait_for(retry, 5)
        await until(lambda: not self.gw.busy)
        self.assertEqual(self.client.posts, [])
        self.assertEqual(self.command_names(), [])


class EventBackpressureTest(srv.GatewayCase):
    async def test_blocked_rejection_log_cannot_stop_active_device_reads(self):
        active = asyncio.create_task(self.gw.handle(self.request()))
        await self.h.feed(gf.ASSISTANT_START)
        await self.h.feed('{"type":"pet.converse.begin"}')
        sink = ge.EventLog(self.base / "events", io.StringIO())
        self.gw._event_log = sink
        entered, release = threading.Event(), threading.Event()
        real_write = os.write
        fd = sink._fd

        def blocked_write(target, data):
            if target == fd:
                entered.set()
                release.wait()
            return real_write(target, data)

        # A watchdog releases a baseline event-loop stall without hanging the
        # suite. The candidate must read audio BEFORE this release, not after.
        watchdog = threading.Timer(2, release.set)
        watchdog.start()
        rejected = None
        try:
            with mock.patch.object(ge.os, "write", side_effect=blocked_write):
                rejected = asyncio.create_task(self.gw.handle(self.request(path="/bad")))
                await until(entered.is_set)
                self.h.device.feed(audio_frame(2400))
                await until(lambda: self.gw._active.outcome.audio_in_bytes == 4800)
                self.assertFalse(release.is_set(),
                                 "regular-file log I/O stalled active device reads")
                self.assertFalse(rejected.done())
                release.set()
                self.assertEqual((await rejected).status, 404)
        finally:
            release.set()
            watchdog.cancel()
            if rejected is not None:
                await rejected
            await self.h.feed('{"type":"pet.stop"}')
            await active
            await self.gw.shutdown()
            sink.close()


class PreDialGatewayTest(srv.GatewayCase):
    async def test_attempt_logging_that_returns_after_start_budget_never_dials(self):
        original = self.gw._event
        async def delayed(event, **facts):
            await original(event, **facts)
            if event == "provider_dial" and facts.get("outcome") == "attempt":
                self.h.clock._now += 6  # before the socket operation, with no timer dispatch
        with mock.patch.object(self.gw, "_event", delayed):
            handle = asyncio.create_task(self.gw.handle(self.request()))
            self.h.device.feed({**json.loads(gf.ASSISTANT_START), "max_seconds": 5})
            await self.h.quiesce()
            self.assertEqual(self.connects, [])
            await asyncio.wait_for(handle, 2)
        self.assertFalse(self.h.device.of_type("session.started"))

    async def test_gate_failure_values_or_exception_emit_policy_without_dial_or_details(self):
        for value in (False, None, 1, {}, RuntimeError(gf.KEY_CANARY)):
            with self.subTest(value=type(value).__name__):
                self.h.ops.pre_dial_safe = (
                    mock.Mock(side_effect=value) if isinstance(value, Exception)
                    else mock.Mock(return_value=value)
                )
                device = gf.FakeDeviceLink(self.h.clock)
                self.devices = [device]
                task = asyncio.create_task(self.gw.handle(self.request()))
                device.feed(gf.ASSISTANT_START)
                await self.h.quiesce()
                await asyncio.wait_for(task, 5)
                await until(lambda: not self.gw.busy)
                self.assertEqual(device.close_codes, [1008])
                self.assertEqual(device.of_type("session.started"), [])
                self.assertEqual(self.connects, [])
                self.assertNotIn(gf.KEY_CANARY, repr(device.frames))
                self.assertNotIn(gf.KEY_CANARY, repr(device.close_codes))

    async def test_shutdown_during_gate_keeps_custody_until_check_settles_no_dial(self):
        release = self.h.ops.hold("pre_dial_safe")
        device = self.h.device
        task = asyncio.create_task(self.gw.handle(self.request()))
        device.feed(gf.ASSISTANT_START)
        await until(lambda: self.h.ops.entered.get("pre_dial_safe", threading.Event()).is_set())
        self.assertEqual((await self.gw.handle(self.request())).status, 409)
        shutdown = asyncio.create_task(self.gw.shutdown())
        await self.h.quiesce()
        self.assertTrue(self.gw.busy)
        self.assertFalse(shutdown.done())
        self.assertEqual(self.connects, [])
        release.set()
        await asyncio.wait_for(shutdown, 5)
        await asyncio.wait_for(task, 5)
        self.assertFalse(self.gw.busy)
        self.assertEqual(self.connects, [])

    async def test_stop_during_gate_waits_for_worker_and_never_starts_provider(self):
        release = self.h.ops.hold("pre_dial_safe")
        device = self.h.device
        task = asyncio.create_task(self.gw.handle(self.request()))
        device.feed(gf.ASSISTANT_START)
        await until(lambda: self.h.ops.entered.get("pre_dial_safe", threading.Event()).is_set())
        device.feed({"type": "pet.stop"})
        await self.h.quiesce()
        await asyncio.wait_for(task, 5)
        self.assertTrue(self.gw.busy)
        self.assertEqual((await self.gw.handle(self.request())).status, 409)
        self.assertEqual(self.connects, [])
        release.set()
        await until(lambda: not self.gw.busy)
        self.assertEqual(self.connects, [])

    async def test_cancelled_http_waiter_keeps_gate_custody_and_no_late_dial(self):
        release = self.h.ops.hold("pre_dial_safe")
        device = self.h.device
        task = asyncio.create_task(self.gw.handle(self.request()))
        device.feed(gf.ASSISTANT_START)
        await until(lambda: self.h.ops.entered.get("pre_dial_safe", threading.Event()).is_set())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.h.quiesce()
        self.assertTrue(self.gw.busy)
        self.assertEqual((await self.gw.handle(self.request())).status, 409)
        self.assertEqual(self.connects, [])
        release.set()
        await until(lambda: not self.gw.busy)
        self.assertEqual(self.connects, [])