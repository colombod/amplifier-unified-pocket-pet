#!/bin/sh
# Run inside the pinned ESP-IDF SDK container, never a host SDK installation.
set -eu
cd "$(dirname "$0")/.."
# Mutant, compile-only, real-parent-only, or optimized Python modes do not
# execute this complete maintained suite. Reject rather than report false PASS.
if [ -n "${PET_CERT_TEST_MUTANT:-}" ] || [ "${PET_CERT_STITCH_ONLY:-0}" != 0 ] ||
   [ "${BRIDGE_JOIN_REAL_PARENT:-0}" != 0 ] || [ -n "${PYTHONOPTIMIZE:-}" ] ||
   [ "${VOICE_CASE:-all}" != all ] || [ -n "${VOICE_SOURCE:-}" ] ||
   [ -n "${WG_RX_SOURCE:-}" ] || [ -n "${AUDIO_LIFECYCLE_MUTANT:-}" ]; then
  printf '%s\n' 'run_native requires full, non-mutant tests with Python assertions enabled' >&2
  exit 2
fi
export PET_REPO="$(cd ../.. && pwd)"
out=${1:-/tmp/pet-native}
mkdir -p "$out"
python3 tests/ui_contract_test.py
python3 tests/input_diag_test.py --out "$out/input-diag"
for name in setup_address live_protocol network_retry; do
  cc -Wall -Wextra -Werror -g -fsanitize=address,undefined -fno-omit-frame-pointer \
    -I main "tests/${name}_test.c" -lm -o "$out/$name"
  "$out/$name"
done
sh tests/run_enrollment.sh "$out/enrollment"
sh tests/run_spike.sh "$out/spike"
sh tests/run_audio_lifecycle.sh "$out/audio-lifecycle"
sh tests/run_voice_pipeline.sh "$out/voice"
sh tests/run_peer_diag.sh "$out/peer-diag"
sh tests/run_tcp.sh "$out/tcp"
sh tests/run_wg_rx.sh "$out/wg-rx"
cc -Wall -Wextra -Werror -g -fsanitize=address,undefined -fno-omit-frame-pointer \
  -I main -I "$IDF_PATH/components/json/cJSON" tests/setup_tailscale_test.c \
  "$IDF_PATH/components/json/cJSON/cJSON.c" -o "$out/setup_tailscale"
"$out/setup_tailscale"
for name in tailnet_storage tailnet_policy; do
  cc -Wall -Wextra -Werror -g -fsanitize=address,undefined -fno-omit-frame-pointer \
    -I main -I "$PET_REPO/components/pet_tls/include" -I "$PET_REPO/components/tailtiny/include" -I "$PET_REPO/components/tailtiny/src" \
    -I "$IDF_PATH/components/json/cJSON" "tests/${name}_test.c" \
    "$IDF_PATH/components/json/cJSON/cJSON.c" "$PET_REPO/components/tailtiny/src/x25519.c" -o "$out/$name"
  "$out/$name"
done
sh tests/run_storage.sh "$out/storage"
for name in setup_bridge bridge_protocol; do
  cc -Wall -Wextra -Werror -g -fsanitize=address,undefined -fno-omit-frame-pointer \
    -I main -I tests/tls_host -I "$PET_REPO/tests/touch_host" \
    -I "$IDF_PATH/components/json/cJSON" -I "$IDF_PATH/components/mbedtls/mbedtls/include" \
    "tests/${name}_test.c" $(test "$name" != bridge_protocol || printf '%s' main/bridge_protocol.c) \
    "$IDF_PATH/components/json/cJSON/cJSON.c" "${PET_TLS_HOST_LIB:?real host mbedcrypto required}/libmbedcrypto.a" -o "$out/$name"
  "$out/$name"
done
sh tests/run_bridge_native.sh "$out/bridge-runtime"
sh tests/run_bridge_converse.sh "$out/bridge-converse"
python3 tests/run_bridge_transport_native.py "$out/bridge-transport"
python3 tests/run_bridge_transport_native.py "$out/bridge-lifecycle" lifecycle
python3 tests/run_bridge_transport_native.py "$out/bridge-receive" receive
# Certificate failures: the adapter matrix consumes synthetic TLS errors using
# exact pinned SSL/WS bodies. The stitch supplies real host mbedTLS flags over
# socketpairs, then crosses synthetic TLS and transport seams into the actual
# conversation owner. Neither test is an IDF-device TLS/RTOS qualification.
python3 tests/run_bridge_transport_native.py "$out/bridge-cert" cert-connect
python3 tests/run_cert_stitch.py "$out/bridge-cert/test" "$out/bridge-converse/converse" "$out/bridge-cert-stitch"
# Setup-page JavaScript suites run separately with the existing host Node.
# Node is intentionally not installed into the pinned offline SDK image.