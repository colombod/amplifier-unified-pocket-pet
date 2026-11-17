#!/usr/bin/env python3
"""Build and run the link rig. Inside the pinned SDK container only; see
run_link_rig.sh for the two container invocations and README.md for scope.

  build <out> <firmware-sdkconfig> [KEY=VALUE ...]   unprivileged, no network
  run   <out> <scenario>[,<scenario>...]|all         root + NET_ADMIN + /dev/net/tun,
                                                     in a private network namespace
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent.parent
TAP = "rigtap0"
# What decides link behaviour on the device. Copied from the firmware
# configuration under test so the rig cannot quietly run a different stack.
MIRRORED = (
    "LWIP_TCP_SND_BUF_DEFAULT", "LWIP_TCP_WND_DEFAULT", "LWIP_TCP_MSS", "LWIP_TCP_RECVMBOX_SIZE",
    "LWIP_TCP_RTO_TIME", "LWIP_TCP_HIGH_SPEED_RETRANSMISSION", "LWIP_TCP_QUEUE_OOSEQ",
    "LWIP_TCP_OVERSIZE_MSS", "LWIP_TCP_TMR_INTERVAL", "LWIP_TCP_MAXRTX", "LWIP_TCPIP_TASK_PRIO",
    "LWIP_TCPIP_RECVMBOX_SIZE", "LWIP_SO_RCVBUF", "LWIP_MAX_SOCKETS", "MBEDTLS_SSL_IN_CONTENT_LEN",
    "MBEDTLS_SSL_OUT_CONTENT_LEN", "MBEDTLS_ASYMMETRIC_CONTENT_LEN", "ESP_WS_CLIENT_SEPARATE_TX_LOCK",
    "ESP_WS_CLIENT_TX_LOCK_TIMEOUT_MS", "WS_BUFFER_SIZE", "FREERTOS_HZ",
)
PRODUCTION = ("bridge_voice.c", "bridge_transport.c", "bridge_protocol.c", "bridge_protocol.h",
              "bridge_transport.h", "bridge_voice.h", "live_protocol.h", "assistant_config.h")


def config_values(path: Path) -> dict[str, str]:
    found = {}
    for line in path.read_text().splitlines():
        match = re.match(r"CONFIG_([A-Z0-9_]+)=(.*)$", line)
        if match:
            found[match.group(1)] = match.group(2)
    return found


def sh(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, **kwargs)


def build(out: Path, firmware_config: Path, overrides: list[str]) -> None:
    project, pki = out / "project", out / "pki"
    for stale in (project, out / "build"):
        shutil.rmtree(stale, ignore_errors=True)
    shutil.copytree(HERE, project, ignore=shutil.ignore_patterns("__pycache__"))
    pki.mkdir(parents=True, exist_ok=True)
    if not (pki / "server.pem").exists():  # a throwaway authority for this output directory only
        quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "cwd": pki}
        curve = ("-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes")
        sh("openssl", "req", "-x509", *curve, "-keyout", "ca.key", "-out", "ca.pem", "-days", "3",
           "-subj", "/CN=pet link rig", "-addext", "basicConstraints=critical,CA:TRUE",
           "-addext", "keyUsage=critical,keyCertSign", **quiet)
        sh("openssl", "req", *curve, "-keyout", "server.key", "-out", "server.csr",
           "-subj", "/CN=pet-rig.example.test", **quiet)
        (pki / "ext.cnf").write_text("subjectAltName=DNS:pet-rig.example.test\nbasicConstraints=CA:FALSE\n"
                                     "keyUsage=digitalSignature\nextendedKeyUsage=serverAuth\n")
        sh("openssl", "x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
           "-CAcreateserial", "-out", "server.pem", "-days", "3", "-extfile", "ext.cnf", **quiet)
    shutil.copy(pki / "ca.pem", project / "rig-ca.pem")
    firmware = config_values(firmware_config)
    wanted = {key: firmware[key] for key in MIRRORED if key in firmware}
    changed = {}
    for item in overrides:
        key, value = item.split("=", 1)
        key = key.removeprefix("CONFIG_")
        changed[key] = value
        wanted[key] = value
    with (project / "sdkconfig.defaults").open("a") as defaults:
        for key, value in wanted.items():
            defaults.write(f"CONFIG_{key}={value}\n")
        defaults.write('CONFIG_MBEDTLS_CUSTOM_CERTIFICATE_BUNDLE_PATH="rig-ca.pem"\n')
    env = dict(os.environ, PET_APP_MAIN=str(APP / "main"),
               PET_WS_CLIENT_DIR=os.environ["PET_WS_CLIENT_DIR"])
    for step in (("--preview", "set-target", "linux"), ("build",)):
        with (out / f"idf-{step[-1]}.log").open("w") as log:
            done = subprocess.run(("idf.py", "-C", str(project), "-B", str(out / "build"), *step),
                                  env=env, stdout=log, stderr=subprocess.STDOUT)
        if done.returncode:
            sys.exit(f"idf.py {step[-1]} failed; see {out}/idf-{step[-1]}.log")
    built = config_values(project / "sdkconfig")
    mismatched = {k: (v, built.get(k)) for k, v in wanted.items() if built.get(k) != v}
    record = {
        "production_sha256": {n: hashlib.sha256((APP / "main" / n).read_bytes()).hexdigest() for n in PRODUCTION},
        "firmware_config_sha256": hashlib.sha256(firmware_config.read_bytes()).hexdigest(),
        "mirrored": {k: built.get(k) for k in wanted},
        "not_in_firmware_config": [k for k in MIRRORED if k not in firmware],
        "overrides": changed,
        "mismatched": mismatched,
    }
    (out / "rig-config.json").write_text(json.dumps(record, indent=2) + "\n")
    if mismatched:
        sys.exit(f"rig stack differs from the requested configuration: {mismatched}")
    print(f"built {out}/build/pet_link_rig.elf; overrides={changed or 'none'}")


# ---- the tap interface, without iproute2 ----
def _ifreq(name: str, payload: bytes) -> bytes:
    return struct.pack("16s", name.encode()) + payload.ljust(24, b"\0")


def tap(create: bool) -> None:
    fd = os.open("/dev/net/tun", os.O_RDWR)
    try:
        fcntl.ioctl(fd, 0x400454CA, _ifreq(TAP, struct.pack("H", 0x0002 | 0x1000)))  # TUNSETIFF tap, no_pi
        fcntl.ioctl(fd, 0x400454CB, 1 if create else 0)  # TUNSETPERSIST
    finally:
        os.close(fd)
    if not create:
        return
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        def address(text: str) -> bytes:
            return struct.pack("HH4s", socket.AF_INET, 0, socket.inet_aton(text))
        fcntl.ioctl(s, 0x8916, _ifreq(TAP, address("192.168.77.1")))   # SIOCSIFADDR
        fcntl.ioctl(s, 0x891C, _ifreq(TAP, address("255.255.255.0")))  # SIOCSIFNETMASK
        fcntl.ioctl(s, 0x8914, _ifreq(TAP, struct.pack("H", 0x1 | 0x40)))  # SIOCSIFFLAGS up|running


def run_one(out: Path, name: str, scenario: dict) -> dict:
    case = out / "runs" / name
    shutil.rmtree(case, ignore_errors=True)
    case.mkdir(parents=True)
    tap(True)
    host = device = None
    try:
        host_log = (case / "host.log").open("w")
        host = subprocess.Popen(
            (sys.executable, str(HERE / "rig_host.py"), str(out / "pki/server.pem"), str(out / "pki/server.key"),
             json.dumps(scenario), str(case / "host.json")), stdout=subprocess.PIPE, stderr=host_log, text=True)
        if host.stdout.readline().strip() != "HOST ready":
            raise RuntimeError("host did not start")
        env = {"RIG_TAP": TAP, "RIG_LINK": scenario.get("link", ""),
               "RIG_MIC_OFF_MS": str(scenario.get("mic_off_ms", 0)),
               "RIG_UI_LOAD_PCT": str(scenario.get("ui_load_pct", 0)),
               "RIG_SEED": str(scenario.get("seed", 8230)),
               "RIG_WATCHDOG_MS": str(scenario.get("watchdog_ms", 90000))}
        with (case / "device.log").open("w") as log:
            device = subprocess.Popen((str(out / "build/pet_link_rig.elf"),), env=env, stdout=log,
                                      stderr=subprocess.STDOUT)
            try:
                code = device.wait(timeout=scenario.get("watchdog_ms", 90000) / 1000 + 20)
            except subprocess.TimeoutExpired:
                device.kill()
                code = -9
        time.sleep(0.3)  # let the host record the end of the connection
    finally:
        if device and device.poll() is None:
            device.kill()
            device.wait()
        if host and host.poll() is None:
            host.terminate()
            try:
                host.wait(timeout=5)
            except subprocess.TimeoutExpired:
                host.kill()
                host.wait()
        tap(False)
    statuses = []
    facts = {}
    probes = []
    for line in (case / "device.log").read_text(errors="replace").splitlines():
        match = re.match(r"RIG status t=(\d+) since_listening_ms=(\d+) state=(\d+) text=(.*)$", line)
        if match:
            statuses.append({"ms": int(match.group(2)), "state": int(match.group(3)), "text": match.group(4)})
        if line.startswith("RIG probe "):
            probes.append({k: int(v) for k, v in (item.split("=") for item in line[10:].split())})
        for kind in ("codec", "final", "link up", "link down", "mic_off"):
            if line.startswith(f"RIG {kind} "):
                facts[kind] = line[len(kind) + 5:]
    host_result = json.loads((case / "host.json").read_text()) if (case / "host.json").exists() else {"calls": []}
    result = {"scenario": name, "definition": scenario, "device_exit": code, "statuses": statuses,
              "device": facts, "probes": probes, "host": host_result}
    result["verdict"] = judge(scenario, result)
    (case / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def judge(scenario: dict, result: dict) -> dict:
    """Required behaviour, the same for every build under test."""
    want = scenario["expect"]
    texts = [s["text"] for s in result["statuses"]]
    calls = result["host"]["calls"]
    first = calls[0] if calls else {}
    problems = []
    if result["device_exit"] != 0:
        problems.append(f"device run did not finish normally (exit {result['device_exit']})")
    if any(c["mic_order_errors"] for c in calls):
        problems.append("microphone stream reached the host out of order or with a gap")
    interrupted = "Microphone stream interrupted." in texts or "Reconnecting..." in texts or len(calls) > 1
    if want == "survives":
        # One call from mic on to the user's mic off, every captured byte delivered once.
        if interrupted:
            problems.append("the call was interrupted: " + " | ".join(t for t in texts if t not in ("Listening", "Speaking")))
        if not texts or texts[-1] != "Mic off":
            problems.append(f"did not end with the user's mic off (last status: {texts[-1] if texts else None})")
        need = scenario["mic_off_ms"] / 1000 - 1.0
        if first.get("mic_seconds", 0) < need:
            problems.append(f"host received {first.get('mic_seconds', 0)} s of microphone, expected at least {need:.1f} s")
    elif want == "survives_or_reconnects":
        # Beyond what one connection can carry. The call may be replaced (the
        # user chose reconnecting while the mic is on); it must still end on the
        # user's mic off, and nothing may arrive twice or out of order.
        if not texts or texts[-1] != "Mic off":
            problems.append(f"did not end with the user's mic off (last status: {texts[-1] if texts else None})")
    else:
        raise ValueError(want)
    return {"pass": not problems, "problems": problems}


# RIG_SCENARIOS names another file for exploration; the maintained set is scenarios.json.
SCENARIOS: dict[str, dict] = json.loads(Path(os.environ.get("RIG_SCENARIOS") or HERE / "scenarios.json").read_text())


def run(out: Path, selection: str) -> None:
    names = list(SCENARIOS) if selection == "all" else selection.split(",")
    results = [run_one(out, name, SCENARIOS[name]) for name in names]
    summary = {"config": json.loads((out / "rig-config.json").read_text()),
               "results": [{"scenario": r["scenario"], "pass": r["verdict"]["pass"],
                            "problems": r["verdict"]["problems"],
                            "last_status": r["statuses"][-1]["text"] if r["statuses"] else None,
                            "calls": len(r["host"]["calls"]),
                            "mic_seconds": [c["mic_seconds"] for c in r["host"]["calls"]],
                            "mic_max_late_seconds": [c["mic_max_late_seconds"] for c in r["host"]["calls"]],
                            "owner_send_max_ms": max((p["owner_send_max_ms"] for p in r["probes"]), default=0),
                            "owner_lock_wait_ms": sum(p["owner_lock_wait_ms"] for p in r["probes"]),
                            "owner_socket_wait_ms": sum(p["owner_socket_wait_ms"] for p in r["probes"]),
                            "sends_failed": sum(p["failed"] for p in r["probes"])}
                           for r in results]}
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    for r in summary["results"]:
        print(f"{'PASS' if r['pass'] else 'FAIL'} {r['scenario']}: last={r['last_status']!r} calls={r['calls']} "
              f"mic_s={r['mic_seconds']} max_late_s={r['mic_max_late_seconds']} send_max_ms={r['owner_send_max_ms']} "
              f"lock_wait_ms={r['owner_lock_wait_ms']} socket_wait_ms={r['owner_socket_wait_ms']} send_failed={r['sends_failed']}"
              + ("" if r["pass"] else " :: " + "; ".join(r["problems"])))
    uid = os.environ.get("RIG_CHOWN")
    if uid:
        sh("chown", "-R", uid, str(out))
    sys.exit(0 if all(r["pass"] for r in summary["results"]) else 1)


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "build":
        build(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4:])
    elif len(sys.argv) == 4 and sys.argv[1] == "run":
        run(Path(sys.argv[2]), sys.argv[3])
    else:
        sys.exit(__doc__)
