#!/bin/sh
# Link rig: the PRODUCTION conversation owner and transport on the pinned
# lwIP/TLS/WebSocket stack, over an impaired link to a scripted host.
# No device, no provider, no Unified, no network beyond a private namespace.
#
#   PET_DEV_IMAGE=<local IDF 5.5.4 image> PET_COMPONENT_CACHE=<verified cache> \
#     sh tests/link_rig/run_link_rig.sh <fresh-absolute-out> <firmware-sdkconfig> <scenarios|all> [KEY=VALUE ...]
#
# KEY=VALUE overrides name a CANDIDATE stack setting; without them the rig
# mirrors the given firmware configuration. Step 2 needs NET_ADMIN and
# /dev/net/tun inside its own container network namespace (--network none).
set -eu
app=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
repo=$(CDPATH= cd -- "$app/../.." && pwd)
out=${1:?fresh absolute output required}; config=${2:?firmware sdkconfig required}; which=${3:?scenario list or all}
shift 3
case "$out" in /*) ;; *) echo "absolute output required" >&2; exit 2;; esac
if [ -e "$out" ]; then echo "fresh output required: $out" >&2; exit 2; fi
image=${PET_DEV_IMAGE:?local IDF 5.5.4 image required}
cache=$(CDPATH= cd -- "${PET_COMPONENT_CACHE:?verified component cache required}" && pwd)
config=$(CDPATH= cd -- "$(dirname "$config")" && pwd)/$(basename "$config")
mkdir -p "$out"
common="--rm --pull never --network none --mount type=bind,src=$repo,dst=/project,readonly \
 --mount type=bind,src=$out,dst=/out --env HOME=/tmp --env PYTHONDONTWRITEBYTECODE=1 \
 --workdir /project/apps/unified-pet/tests/link_rig"
# 1. Build: unprivileged, no network.
docker run $common --user "$(id -u):$(id -g)" --cap-drop ALL --security-opt no-new-privileges \
  --mount "type=bind,src=$cache/esp_websocket_client,dst=/pinned/esp_websocket_client,readonly" \
  --mount "type=bind,src=$config,dst=/firmware-sdkconfig,readonly" \
  --env PET_WS_CLIENT_DIR=/pinned/esp_websocket_client "$image" \
  python3 run_link_rig.py build /out /firmware-sdkconfig "$@"
# 2. Run: root inside a private, otherwise empty network namespace, for the tap device.
docker run $common --cap-drop ALL --cap-add NET_ADMIN --cap-add CHOWN --cap-add DAC_OVERRIDE --device /dev/net/tun \
  --env RIG_CHOWN="$(id -u):$(id -g)" "$image" \
  python3 run_link_rig.py run /out "$which"
