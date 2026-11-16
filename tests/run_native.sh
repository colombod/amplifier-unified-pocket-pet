#!/bin/sh
# Run inside the pinned ESP-IDF SDK container, never a host SDK installation.
set -eu
cd "$(dirname "$0")/.."
out=${1:-/tmp/pet-native}
mkdir -p "$out"
for name in setup_address live_protocol touch_alignment network_retry; do
  cc -Wall -Wextra -Werror -g -fsanitize=address,undefined -fno-omit-frame-pointer \
    -I main -I components/pet_touch_calibration/include "tests/${name}_test.c" -lm -o "$out/$name"
  "$out/$name"
done
sh tests/run_enrollment.sh "$out/enrollment"
sh tests/run_spike.sh "$out/spike"
sh tests/run_audio_lifecycle.sh "$out/audio-lifecycle"
sh tests/run_peer_diag.sh "$out/peer-diag"
sh tests/run_tcp.sh "$out/tcp"
sh tests/run_wg_rx.sh "$out/wg-rx"
cc -Wall -Wextra -Werror -g -fsanitize=address,undefined -fno-omit-frame-pointer \
  -I main -I "$IDF_PATH/components/json/cJSON" tests/setup_tailscale_test.c \
  "$IDF_PATH/components/json/cJSON/cJSON.c" -o "$out/setup_tailscale"
"$out/setup_tailscale"
for name in tailnet_storage tailnet_policy tailnet_http; do
  cc -Wall -Wextra -Werror -g -fsanitize=address,undefined -fno-omit-frame-pointer \
    -I main -I components/pet_tls/include -I components/tailtiny/include -I components/tailtiny/src \
    -I "$IDF_PATH/components/json/cJSON" "tests/${name}_test.c" \
    "$IDF_PATH/components/json/cJSON/cJSON.c" components/tailtiny/src/x25519.c -o "$out/$name"
  "$out/$name"
done