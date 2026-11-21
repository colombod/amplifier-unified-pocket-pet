#!/usr/bin/env python3
"""Check the local Unified installation before pairing the pet. No HTTP or device use.

Run with the Python interpreter that serves your Unified installation, not the
gateway environment. This checks package identity and four companion request
shapes against the installed policy. It does not exercise a handler, pairing,
provider, live permissions, work execution or voice.
"""

import argparse
import asyncio
import json
import sys
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

TARGET = "0.116.0"  # candidate target, not a voice-qualified release
SESSION = "pocket-check"
COMMAND = "11111111-1111-4111-8111-111111111111"


def requests():
    # Use the gateway's own builders instead of copying their shapes into this test.
    from pet_bridge import auth, transport, wire

    attach = transport.route_attach()
    listing = transport.route_sessions_observation()
    detail = transport.route_detail(SESSION)
    method, path, body = wire.send_command_request(SESSION, COMMAND, "Compatibility check.")
    return (
        ("attach", attach.method, attach.target, auth.native_attach_payload("example-client")),
        ("list", listing.method, listing.target, None),
        ("read selected chat", detail.method, detail.target, None),
        ("send to selected chat", method, path, json.loads(body)),
    )


async def policy_results(permits):
    from multidict import MultiDict
    from urllib.parse import parse_qsl

    results = []
    for label, method, target, body in requests():
        parts = urlsplit(target)

        async def json_body(payload=body):
            return payload

        request = SimpleNamespace(method=method, path=parts.path,
                                  query=MultiDict(parse_qsl(parts.query)), json=json_body)
        results.append((label, await permits(request) is True))
    return results


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        dist = metadata.distribution("amplifier-unified")
    except metadata.PackageNotFoundError:
        print("No amplifier-unified distribution in this interpreter. Use your Unified service's Python.")
        return 2
    if dist.version != TARGET:
        print(f"Not the candidate target ({TARGET}); this installation has not been checked.")
        return 1
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        from amplifier_web import __version__, device_pairing

        policy_file = Path(device_pairing.__file__).resolve(strict=True)
        expected = Path(dist.locate_file("amplifier_web/device_pairing.py")).resolve(strict=True)
        if __version__ != TARGET or policy_file != expected:
            print("Imported Unified package does not match the target distribution.")
            return 1
        result = asyncio.run(policy_results(device_pairing.permits_companion))
    except (ImportError, AttributeError, TypeError, ValueError, OSError, RuntimeError):
        print("Cannot check the installed companion policy. No request was sent.")
        return 1
    print(f"Unified {TARGET} — local package policy only")
    for label, allowed in result:
        print(f"  {label}: {'allowed' if allowed else 'refused'}")
    if not all(allowed for _, allowed in result):
        print("The installed policy does not admit the gateway's required requests.")
        return 1
    print("All four request shapes are allowed. Pairing, delivery and physical voice were not tested.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())