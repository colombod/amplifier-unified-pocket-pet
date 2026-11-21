#!/bin/sh
# Opt-in host-side qualification. No device, model, provider or published port.
# Use a local Debian-based Python image compatible with the installed Unified
# package tree (including native extensions, aiohttp and PAM). No pulls.
set -eu
tools=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
bridge=$(CDPATH= cd -- "$tools/.." && pwd)
site=${UNIFIED_SITE:?absolute directory containing the installed Unified package and its dependencies}
image=${PYTHON_IMAGE:?local Python image matching the Unified package ABI}
case "$site" in /*) ;; *) echo 'UNIFIED_SITE must be absolute' >&2; exit 2;; esac
test -f "$site/amplifier_web/server.py"
exec docker run --rm --pull never --network none --pids-limit 160 --cpus 2 --memory 1g \
  --label purpose=pocket-disposable-onboarding \
  --mount "type=bind,src=$bridge/pet_bridge,dst=/product/bridge/pet_bridge,readonly" \
  --mount "type=bind,src=$tools/test_local_unified_pairing.py,dst=/product/bridge/tools/test_local_unified_pairing.py,readonly" \
  --mount "type=bind,src=$site,dst=/unified-runtime,readonly" \
  --env PYTHONPATH=/unified-runtime --env PYTHONDONTWRITEBYTECODE=1 --env POCKET_DISPOSABLE_HOST=1 \
  "$image" sh -ceu '
    umask 077
    useradd -m -s /bin/sh pocketfixture
    install -d -o pocketfixture -g pocketfixture -m 700 /tmp/pocket-owner
    openssl rand -hex 32 > /tmp/pocket-owner/password
    printf "pocketfixture:%s\n" "$(cat /tmp/pocket-owner/password)" | chpasswd
    openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=localhost \
      -addext subjectAltName=DNS:localhost -addext basicConstraints=critical,CA:TRUE \
      -keyout /tmp/pocket-owner/key -out /tmp/pocket-owner/cert 2>/dev/null
    install -m 644 /tmp/pocket-owner/cert /usr/local/share/ca-certificates/pocket-fixture.crt
    update-ca-certificates >/dev/null 2>&1
    chmod 644 /etc/ssl/certs/ca-certificates.crt
    chown -R pocketfixture:pocketfixture /tmp/pocket-owner
    exec runuser -u pocketfixture -- python -B -P /product/bridge/tools/test_local_unified_pairing.py \
      --cert /tmp/pocket-owner/cert --key /tmp/pocket-owner/key --password-file /tmp/pocket-owner/password
  '