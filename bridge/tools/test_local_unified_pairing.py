"""Opt-in onboarding integration check on a *disposable, loopback-only* Linux host.

Run as that host's throwaway OS account with Unified installed, a trusted
localhost TLS certificate, and a private file containing that account's password.
There is no remote-host option. Real login, approval, HTTPS, credential store,
catalog and binding; no voice provider or model execution. Never use live keys.
"""
import argparse
import asyncio
import os
from pathlib import Path
import pwd
import re
import ssl
import sys
import uuid
from tempfile import TemporaryDirectory

BRIDGE = Path(__file__).resolve().parents[1]
ORIGIN = "https://localhost:8941"


class NoModel:
    def __init__(self):
        self.attempts = 0

    async def start(self, *args, **kwargs):
        self.attempts += 1
        raise AssertionError("No model work permitted in setup verification")

    send = start

    async def close(self):
        pass


async def gateway(*args, env):
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-B", "-m", "pet_bridge", *args, cwd=BRIDGE, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 18)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode:
        # Never forward CLI stdout: the pairing prompt may contain private data.
        detail = re.search(rb"error: ([a-z_]+)", stderr)
        reason = detail[1].decode() if detail else "command failed"
        raise AssertionError(f"Gateway {args[0]}: {reason}")
    return stdout.decode()


def form_csrf(page):
    match = re.search(r'name="csrf" value="([^"]+)"', page)
    if not match:
        raise AssertionError("Missing expected form")
    return match[1]


async def verify(home, args):
    import aiohttp
    from aiohttp import web
    from amplifier_web.server import create_app
    from amplifier_web import __version__
    from pet_bridge.journal import BINDING_NAME, Journal
    from pet_bridge.auth import default_root

    workspace = home / "workspaces"
    workspace.mkdir()
    env = dict(os.environ, XDG_CONFIG_HOME=str(home / "gateway-config"))
    Path(env["XDG_CONFIG_HOME"]).mkdir(mode=0o700)
    account = pwd.getpwuid(os.geteuid()).pw_name
    password = args.password_file.read_text().strip()
    runtime = NoModel()
    app = await create_app(home / "host", workspace=workspace, runtime=runtime, voice=False,
                           background_work=False, background_updates=False, preload_providers=False)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(args.cert, args.key)
        await web.TCPSite(runner, "127.0.0.1", 8941, ssl_context=tls).start()
        async with aiohttp.ClientSession(
            base_url=ORIGIN, trust_env=False, headers={"Origin": ORIGIN},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as owner:
            async with owner.get("/login") as response:
                assert response.status == 200, ("login page", response.status)
                proof = form_csrf(await response.text())
            async with owner.post("/login", data={"username": account, "password": password,
                                  "csrf": proof}, allow_redirects=False) as response:
                assert response.status == 303 and response.headers["Location"] == "/", "Login failed"
            del password
            async with owner.post("/api/clients/attach", json={"kind": "web", "clientId": "setup-owner",
                                  "protocolVersion": 1, "includeState": False}) as response:
                assert response.status == 200, ("owner attach", response.status)
            ids = {}
            for name in ("Orchid", "Harbor"):
                folder = workspace / name
                folder.mkdir()
                sid = str(uuid.uuid4())
                async with owner.post("/api/actions", headers={"X-Amplifier-Client": "setup-owner"},
                                      json={"id": str(uuid.uuid4()), "action": "session.create",
                                            "args": {"id": sid, "title": name, "workspace": str(folder),
                                                     "select": False}}) as response:
                    body = await response.json()
                    assert response.status == 200 and body.get("accepted"), ("seed chat", response.status)
                assert app["service"]._session(sid)["workspace"] == str(folder)
                ids[name] = sid
            prompt = await gateway("pair-start", "--origin", ORIGIN, "--name", "Pocket fixture", env=env)
            url = next(line for line in prompt.splitlines() if line.startswith(ORIGIN + "/devices/pair?id="))
            code = next(line.split(": ", 1)[1] for line in prompt.splitlines() if "this code matches:" in line)
            target = url[len(ORIGIN):]
            async with owner.get(target, allow_redirects=False) as response:
                assert response.status == 200, ("approval page", response.status)
                page = await response.text()
                assert f"<code>{code}</code>" in page, "Owner and gateway comparison codes differ"
                proof = form_csrf(page)
            async with owner.post(target, data={"csrf": proof, "decision": "allow"},
                                  allow_redirects=False) as response:
                assert response.status == 200 and "Connection approved" in await response.text()
            print(f"Unified {__version__}: disposable owner login and matching-code approval passed.", flush=True)
            poll = await gateway("pair-poll", env=env)
            if m := re.fullmatch(r"Too early\. Try again in (\d+) seconds\.\s*", poll):
                delay = int(m[1])
                assert 0 < delay <= 5
                await asyncio.sleep(delay)  # the interval returned by this pairing, not a new start
                poll = await gateway("pair-poll", env=env)
            assert "Approved. Credential reference:" in poll
            assert "Attached." in await gateway("attach", env=env)
            listing = await gateway("list", env=env)
            assert "Sessions: complete;" in listing
            for name, sid in ids.items():
                assert any(sid[-8:] in line and line.endswith(name) for line in listing.splitlines()), name
                scoped = await gateway("list", "--all-work", "--workspace", str(workspace / name), env=env)
                assert "complete;" in scoped and "Filtered to the requested workspace." in scoped
                assert any(sid[-8:] in line and line.endswith(name) for line in scoped.splitlines()), name
                assert not any(other[-8:] in scoped for other in ids.values() if other != sid)
            for name in ("Orchid", "Harbor", "Orchid"):
                sid = ids[name]
                assert f"...{sid[-8:]} ({name})" in await gateway("bind", sid, env=env)
                with Journal(default_root(env) / "journal") as journal:
                    assert journal.get(BINDING_NAME)["binding"]["session_id"] == sid
            assert runtime.attempts == 0
            print("Gateway redeemed its restricted credential, attached, found two existing unpinned chats "
                  "in different workspaces, and bound A/B/A to the exact original IDs.")
            print("No messages sent, model work started, paid calls made or pet accessed. "
                  "Not a spoken-context, independent human, or hardware acceptance test.")
    finally:
        await runner.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("cert", "key", "password-file"):
        parser.add_argument("--" + flag, type=Path, required=True)
    args = parser.parse_args()
    if (not __debug__ or os.geteuid() == 0 or not Path("/.dockerenv").exists()
            or {p.name for p in Path("/sys/class/net").iterdir()} != {"lo"}
            or os.environ.get("POCKET_DISPOSABLE_HOST") != "1"):
        parser.error("Use a disposable non-root, network-none test container; never the live host.")
    if args.password_file.stat().st_mode & 0o077 or args.password_file.stat().st_uid != os.geteuid():
        parser.error("Use an owner-private throwaway password file.")
    with TemporaryDirectory(prefix="pocket-unified-fixture-") as td:
        home = Path(td)
        os.chdir(home)
        os.environ.update(HOME=td, AMPLIFIER_HOME=str(home / "amplifier"), AMPLIFIER_WEB_HOME=str(home / "host"),
                          XDG_CONFIG_HOME=str(home / "config"), XDG_STATE_HOME=str(home / "state"))
        sys.path.insert(0, str(BRIDGE))
        asyncio.run(verify(home, args))


if __name__ == "__main__":
    main()