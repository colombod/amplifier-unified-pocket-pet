"""Offline production TLS over socketpairs, real mbedTLS + Python/OpenSSL.

Only ESP time/RNG/task/trust-bundle hardware seams are substituted. Synthetic
test keys stay in the disposable SDK container's output directory, never NVS.
"""

import json
import pathlib
import socket
import ssl
import subprocess
import sys
import threading
import time

binary = pathlib.Path(sys.argv[1]).resolve()
out = pathlib.Path(sys.argv[2]).resolve()
out.mkdir(exist_ok=False)


def openssl(*args):
    subprocess.run(
        ["openssl", *args],
        cwd=out,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


openssl(
    "req",
    "-x509",
    "-newkey",
    "rsa:2048",
    "-nodes",
    "-days",
    "1",
    "-keyout",
    "ca.key",
    "-out",
    "ca.pem",
    "-subj",
    "/CN=Offline Test Root",
    "-addext",
    "basicConstraints=critical,CA:TRUE",
)
openssl(
    "req",
    "-new",
    "-newkey",
    "rsa:2048",
    "-nodes",
    "-keyout",
    "leaf.key",
    "-out",
    "leaf.csr",
    "-subj",
    "/CN=node.example",
)
(out / "extensions").write_text(
    "subjectAltName=DNS:node.example\nbasicConstraints=CA:FALSE\n"
    "keyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n"
)
openssl(
    "x509",
    "-req",
    "-in",
    "leaf.csr",
    "-CA",
    "ca.pem",
    "-CAkey",
    "ca.key",
    "-CAcreateserial",
    "-days",
    "1",
    "-extfile",
    "extensions",
    "-out",
    "leaf.pem",
)
(out / "index").write_text("")
(out / "serial").write_text("1000\n")
(out / "ca.cnf").write_text(
    "[ca]\ndefault_ca=local\n[local]\ndatabase=index\nserial=serial\n"
    "new_certs_dir=.\ncertificate=ca.pem\nprivate_key=ca.key\n"
    "default_md=sha256\npolicy=policy\nx509_extensions=ext\n"
    "[policy]\ncommonName=supplied\n[ext]\nsubjectAltName=DNS:node.example\n"
    "basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\n"
    "extendedKeyUsage=serverAuth\n"
)
openssl(
    "ca",
    "-batch",
    "-config",
    "ca.cnf",
    "-in",
    "leaf.csr",
    "-out",
    "expired.pem",
    "-startdate",
    "20200101000000Z",
    "-enddate",
    "20210101000000Z",
)
openssl(
    "req",
    "-x509",
    "-newkey",
    "rsa:2048",
    "-nodes",
    "-days",
    "1",
    "-keyout",
    "other.key",
    "-out",
    "other.pem",
    "-subj",
    "/CN=node.example",
    "-addext",
    "subjectAltName=DNS:node.example",
)


def trial(
    name, cert="leaf.pem", host="node.example", payload=None, cancel=0, hang=False,
    deadline=1000, exchange_bytes=None
):
    client, server = socket.socketpair()
    request = []
    release_peer = threading.Event()
    peer_closed = threading.Event()

    def serve():
        try:
            if hang:
                # Only the caller releases this peer after the client returned.
                # Three-second safety cap also bounds deadline-removal mutants.
                release_peer.wait(3)
                return
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(
                out / cert, out / ("other.key" if cert == "other.pem" else "leaf.key")
            )
            with ctx.wrap_socket(server, server_side=True) as stream:
                stream.settimeout(2)
                data = b""
                while (len(data) < exchange_bytes if exchange_bytes is not None
                       else b"\r\n\r\n" not in data):
                    part = stream.recv(4096)
                    if not part:
                        return
                    data += part
                request.append(data)
                body = (
                    payload
                    or b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
                )
                for start in range(0, len(body), 7):
                    stream.sendall(body[start : start + 7])
        except (ssl.SSLError, OSError):
            pass
        finally:
            server.close()
            peer_closed.set()

    thread = threading.Thread(target=serve)
    thread.start()
    result = subprocess.run(
        [
            str(binary),
            str(client.fileno()),
            str(out / "ca.pem"),
            host,
            str(deadline),
            str(cancel),
        ] + (["exchange", str(exchange_bytes)] if exchange_bytes is not None else []),
        pass_fds=(client.fileno(),),
        capture_output=True,
        check=False,
        text=True,
        timeout=6,
    )
    closed_before_return = peer_closed.is_set()
    release_peer.set()
    client.close()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert result.returncode == 0, (name, result.returncode, result.stderr)
    value = json.loads(result.stdout)
    assert value["fd_open"], (name, "borrowed fd was closed", value)
    assert value["request_unchanged"], (name, "borrowed request mutated")
    value["peer_closed_before_return"] = closed_before_return
    print(name, json.dumps(value, sort_keys=True), flush=True)
    return value, request


valid, requests = trial("valid-partial-401")
assert (
    valid["result"] == 0
    and valid["verified"]
    and valid["http"]
    and valid["status"] == 401
)
assert requests == [
    (
        b"GET /api/connections/v1 HTTP/1.1\r\nHost: node.example:8941\r\n"
        b"Connection: close\r\nAccept: application/json\r\n\r\n"
    )
]
for name, cert, host in [
    ("wrong-host", "leaf.pem", "wrong.example"),
    ("untrusted", "other.pem", "node.example"),
    ("expired", "expired.pem", "node.example"),
]:
    value, requests = trial(name, cert, host)
    assert value["result"] != 0 and not value["verified"] and not value["http"]
    assert value["verify_flags"] and not requests
for name, payload in [
    ("redirect", b"HTTP/1.1 302 Found\r\nContent-Length: 0\r\n\r\n"),
    ("oversize", b"HTTP/1.1 200 OK\r\nContent-Length: 4097\r\n\r\n"),
    ("truncated", b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n{}"),
]:
    value, _ = trial(name, payload=payload)
    assert value["verified"] and not value["http"] and value["result"] != 0
for total in (4095,4096,4097):
    # Fixed-width length makes the actual header+body byte boundary exact.
    header=b"HTTP/1.1 200 OK\r\nContent-Length: %04d\r\n\r\n"
    body_length=total-len(header % 0)
    payload=(header % body_length)+b"x"*body_length
    assert len(payload)==total and body_length<4096
    value,_=trial(f"actual-response-{total}",payload=payload,deadline=2000)
    assert value["verified"]
    if total<=4096:
        assert value["result"]==0 and value["http"] and value["bytes"]==total
    else:
        assert value["result"]==0x104 and not value["http"] and value["bytes"]==4096
for length in (1,255):
    value,requests=trial(f"caller-request-{length}",exchange_bytes=length)
    assert value["result"]==0 and value["http"] and requests==[b"Q"*length]
for length in (0,256,-1):
    value,requests=trial(f"invalid-request-{length}",exchange_bytes=length)
    assert value["result"]==0x102 and not value["verified"] and not requests
for name,cancel,deadline in (("cancel-during-handshake",80,1000),
                             ("deadline-during-handshake",0,80)):
    value,requests=trial(name,hang=True,cancel=cancel,deadline=deadline)
    assert value["result"]==0x107 and not value["verified"] and not requests, (name,value)
    # 10-ms production polling, wide scheduling allowance, still far below peer close.
    assert 40_000<=value["elapsed_us"]<800_000, (name,value)
    assert not value["peer_closed_before_return"], (name,value)
print(
    "PASS real production TLS: 17 trials; chain/hostname/expiry, adapter/caller request bounds, actual 4096-byte response boundary, timeout-before-peer-close, immutable request and borrowed fd survival"
)
