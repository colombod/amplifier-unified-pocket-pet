"""Offline certificate-result stitch. Run in the pinned network-none SDK only.

Real host mbedTLS/Python OpenSSL use socketpairs and synthetic certificates in
a fresh temporary directory. Their verification flags feed the exact pinned
SDK SSL-connect/error-copy + WS/app adapter fixture, then its actual terminal
result feeds the whole production voice owner with synthetic RTOS/transport.
This is an explicitly one-way seam: it is not a single real IDF TLS handshake,
real FreeRTOS/device execution, or an integrated hardware qualification.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

adapter, owner, root = (Path(x).resolve() for x in sys.argv[1:])
root.mkdir(exist_ok=False)
repo = Path("../..").resolve()
sdk = Path(os.environ["IDF_PATH"])
lib = Path(os.environ["PET_TLS_HOST_LIB"])
inputs = [
    repo / "tests/tailnet_tls_test.py",
    repo / "tests/tailnet_tls_host.c",
    repo / "main/tailnet_tls.c",
    repo / "components/pet_tls/pet_tls.c",
    repo / "components/pet_tls/include/pet_tls.h",
    sdk / "components/mbedtls/mbedtls/include/mbedtls/build_info.h",
    sdk / "components/mbedtls/mbedtls/include/mbedtls/x509.h",
    lib / "libmbedtls.a", lib / "libmbedx509.a", lib / "libmbedcrypto.a",
    adapter, owner, Path(__file__),
]
def hashes():
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}

before = hashes()
helper = root / "host-tls"
cmd = [
    "cc", "-Wall", "-Wextra", "-Werror", "-g", "-fsanitize=address,undefined",
    "-fno-omit-frame-pointer", "-fno-pie", "-no-pie",
    "-I", str(repo / "tests/tls_host"), "-I", str(repo / "main"),
    "-I", str(repo / "components/pet_tls/include"),
    "-I", str(repo / "components/tailtiny/include"),
    "-I", str(sdk / "components/mbedtls/mbedtls/include"),
    str(repo / "tests/tailnet_tls_host.c"), str(repo / "main/tailnet_tls.c"),
    str(repo / "components/pet_tls/pet_tls.c"),
    str(lib / "libmbedtls.a"), str(lib / "libmbedx509.a"),
    str(lib / "libmbedcrypto.a"), "-o", str(helper),
]
subprocess.run(cmd, check=True)
run = subprocess.run(
    [sys.executable, str(repo / "tests/tailnet_tls_test.py"), str(helper), str(root / "fixtures")],
    capture_output=True, text=True, check=True, timeout=120,
)
(root / "tls-output.txt").write_text(run.stdout)
observed = {}
for line in run.stdout.splitlines():
    if " " in line:
        name, rest = line.split(" ", 1)
        if name in ("valid-partial-401", "wrong-host", "untrusted", "expired"):
            observed[name] = json.loads(rest)
assert set(observed) == {"valid-partial-401", "wrong-host", "untrusted", "expired"}
assert observed["valid-partial-401"]["verified"] and not observed["valid-partial-401"]["verify_flags"]
header = (sdk / "components/mbedtls/mbedtls/include/mbedtls/x509.h").read_text()
expected_flags = {}
for name, symbol in (
    ("wrong-host", "MBEDTLS_X509_BADCERT_CN_MISMATCH"),
    ("untrusted", "MBEDTLS_X509_BADCERT_NOT_TRUSTED"),
    ("expired", "MBEDTLS_X509_BADCERT_EXPIRED"),
):
    match = re.search(r"^#define\s+" + symbol + r"\s+(0x[0-9a-fA-F]+)\b", header, re.MULTILINE)
    assert match, symbol
    expected_flags[name] = int(match.group(1), 16)
results = {}
for name in ("wrong-host", "untrusted", "expired"):
    value = observed[name]
    flags = value["verify_flags"]
    assert 0 < flags < 0xFFFFFFFF and not value["verified"] and not value["http"]
    assert flags & expected_flags[name], (name, flags, expected_flags[name])
    transport = subprocess.run(
        [str(adapter), "cert-positive", str(flags)], capture_output=True, text=True,
        check=True, timeout=10,
    )
    lines = [line for line in transport.stdout.splitlines() if line.startswith("CERT_TERMINAL=")]
    assert len(lines) == 1
    terminal = lines[0].split("=", 1)[1]
    env = dict(os.environ, CERT_STITCH_TERMINAL=terminal)
    voice = subprocess.run(
        [str(owner), "cert-stitch"], env=env, capture_output=True, text=True,
        check=True, timeout=10,
    )
    assert "PASS cert-stitch" in voice.stdout
    results[name] = {"flags": flags, "required_flag": expected_flags[name],
                     "terminal": int(terminal), "owner_pass": True}
assert before == hashes(), "stitch inputs changed during execution"
(root / "receipt.json").write_text(json.dumps({
    "inputs": before, "observed": observed, "stitched_results": results,
    "scope": "real host mbedTLS socketpairs -> synthetic IDF TLS seam -> exact SDK SSL/WS/adapter -> synthetic transport seam -> actual voice owner; not real IDF TLS or hardware",
}, indent=2) + "\n")
print("PASS certificate stitch:", json.dumps(results, sort_keys=True), flush=True)