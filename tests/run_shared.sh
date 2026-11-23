#!/bin/sh
# Pinned SDK runtime, real shared production plus hardware-only host seams.
set -eu
cd "$(dirname "$0")/.."
out=${1:?output required}
mkdir -p "$out"
for mode in root app; do
  store=
  if [ "$mode" = app ]; then store='-DPET_TOUCH_STORE_NAMESPACE="unified_pet" -DTEST_APP_STORE'; fi
  cc -Wall -Wextra -Werror -Wno-unused-parameter -Wno-unused-variable \
    -g -fsanitize=address,undefined -fno-omit-frame-pointer \
    -I tests/touch_host -I tests/tls_host -I components/pet_touch_calibration/include \
    $store tests/touch_calibration_test.c -lm -o "$out/touch-$mode"
  "$out/touch-$mode"
done
for name in touch_alignment tailnet_http; do
  cc -Wall -Wextra -Werror -g -fsanitize=address,undefined \
    -I components/pet_touch_calibration/include -I components/pet_tls/include \
    "tests/${name}_test.c" -lm -o "$out/$name"
  "$out/$name"
done
# Host mbedTLS is supplied by the existing approved offline SDK cache.
lib=${PET_TLS_HOST_LIB:?path to real host-built mbedTLS libraries required}
cc -Wall -Wextra -Werror -g -fsanitize=address,undefined -fno-omit-frame-pointer \
  -I tests/tls_host -I main -I components/pet_tls/include -I components/tailtiny/include \
  -I "$IDF_PATH/components/mbedtls/mbedtls/include" \
  tests/tailnet_tls_host.c main/tailnet_tls.c components/pet_tls/pet_tls.c \
  "$lib/libmbedtls.a" "$lib/libmbedx509.a" "$lib/libmbedcrypto.a" -o "$out/tls"
python3 tests/tailnet_tls_test.py "$out/tls" "$out/tls-fixtures"