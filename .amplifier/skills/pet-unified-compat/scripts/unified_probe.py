#!/usr/bin/env python3
"""Read-only check that this gateway source can talk to the running Unified.

Uses the gateway's own config and credential, lists sessions exactly as the idle
check does, and reads the bound conversation's messages. Sends nothing.

The credential store has a single owner: stop the idle gateway first, run this,
start the gateway again. Prints fixed words, counts and status flags only.
Exit 0: compatible and the bound conversation is idle. 1: see the printed reason.
"""
import argparse
import sys
from pathlib import Path

sys.dont_write_bytecode = True


def main():
    here = Path(__file__).resolve()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', required=True, help='absolute path of the gateway config (JSON)')
    parser.add_argument('--bridge', default=str(here.parents[4] / 'bridge'),
                        help='bridge source directory to check (default: this repository)')
    args = parser.parse_args()
    sys.path.insert(0, args.bridge)
    from pet_bridge import auth, companion, gateway_server, transport
    try:
        cfg = gateway_server.load_config(Path(args.config))
    except gateway_server.StartupError as exc:
        print('config refused:', exc)
        return 1
    root = Path(cfg.state_root)
    try:
        store = auth.CredentialStore(Path(cfg.auth_root) if cfg.auth_root is not None else root / 'auth')
    except auth.AuthError as exc:
        print('credential store unavailable (%s): stop the idle gateway, rerun, start it again' % exc)
        return 1
    ok = True
    try:
        attachment = store.load_attachment(cfg.credential_ref)
        if attachment is None:
            print('no attachment for the configured credential: pair and attach first')
            return 1
        client = transport.HttpsClient()
        try:
            listing = companion.list_sessions(client, attachment)
            row = next((r for r in listing.sessions if r.id == cfg.session_id), None)
            print('session list: ok, %d sessions, complete=%s' % (len(listing.sessions), listing.complete))
            if row is None:
                print('bound conversation: NOT LISTED (is its workspace folder registered in Unified?)')
                ok = False
            else:
                verdict = companion.preflight(listing, cfg.session_id)
                print('bound conversation: status=%s idle=%s reason=%s' % (row.status, verdict.idle, verdict.reason))
                ok = ok and verdict.idle
        except companion.CompanionError as err:
            print('session list: REFUSED code=%s http=%s (403/404: check permits_companion in this Unified)'
                  % (err.code, getattr(err, 'status', None)))
            ok = False
        try:
            window = companion.detail_window(client, attachment, cfg.session_id)
            print('messages: ok, %d rows, complete=%s' % (len(window.items), window.complete))
        except companion.CompanionError as err:
            print('messages: REFUSED code=%s http=%s' % (err.code, getattr(err, 'status', None)))
            ok = False
    finally:
        for name in ('close', 'release'):
            if hasattr(store, name):
                getattr(store, name)()
                break
    print('result:', 'compatible, idle' if ok else 'NOT ready')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
