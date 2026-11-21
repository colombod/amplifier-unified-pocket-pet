#!/bin/sh
# No automatic Docker build/pull, resolver upgrade or SDK installation.
# Portable offline build (cache is the approved namespace-stripped component tree):
# PET_DEV_IMAGE=<local-IDF-5.5.4-plus-Pillow-11.3.0-image> \
# PET_COMPONENT_CACHE=/absolute/path/to/offline_components \
#   sh scripts/build.sh --offline-cache
# Cache is mounted read-only and every component's complete content is verified.
# No registry solving, evidence-directory hooks, root assets or root app imports.
set -eu
app=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
repo=$(CDPATH= cd -- "$app/../.." && pwd)
diag=${PET_PRIVATE_MIC_DIAG:-0}
case "$diag" in 0|1) ;; *) echo 'PET_PRIVATE_MIC_DIAG must be 0 or 1' >&2; exit 2;; esac
if [ "${1:-}" != --inside-sdk ]; then
  case "${1:-}" in ""|--offline-cache) ;; *) printf '%s\n' 'Usage: build.sh [--offline-cache]' >&2; exit 2;; esac
  image=${PET_DEV_IMAGE:-esp32-pet-dev:5.5.4}
  if ! command -v docker >/dev/null 2>&1; then
    printf '%s\n' 'Docker CLI not found; install Docker before preparing the tools image' >&2
    exit 1
  fi
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    if ! docker info --format '{{.ServerVersion}}' >/dev/null; then
      printf '%s\n' 'Docker daemon unavailable; verify your Docker context and access' >&2
    else
      printf '%s\n' "Cannot inspect local image $image (IDF 5.5.4 + Pillow 11.3.0); check the image name and local cache" >&2
    fi
    printf '%s\n' 'No build or pull attempted' >&2
    exit 1
  fi
  offline=0
  if [ "${1:-}" = --offline-cache ]; then
    offline=1
    test -n "${PET_COMPONENT_CACHE:-}" || { printf '%s\n' 'PET_COMPONENT_CACHE is required' >&2; exit 2; }
  fi
  # Use the actual local image platform, not the root Compose's Mac arm64 host pin.
  # Target remains ESP32-S3. Root is read-only; only this app can receive outputs.
  set -- docker create --pull never --network none \
    --memory 4g --memory-swap 4g --cpus 3 --pids-limit 512 \
    --cap-drop ALL --security-opt no-new-privileges \
    --user "$(id -u):$(id -g)" \
    --label "owner=${PET_BUILD_OWNER:-local-user}" \
    --label purpose=unified-pet-offline-build \
    --mount "type=bind,src=$repo,dst=/project,readonly" \
    --mount "type=bind,src=$app,dst=/project/apps/unified-pet" \
    --env CCACHE_DIR=/tmp/pet-ccache --env PYTHONDONTWRITEBYTECODE=1 --env "PET_PRIVATE_MIC_DIAG=$diag" \
    --workdir /project/apps/unified-pet
  if [ -n "${PET_COMPONENT_CACHE:-}" ]; then
    cache=$(CDPATH= cd -- "$PET_COMPONENT_CACHE" && pwd)
    set -- "$@" --mount "type=bind,src=$cache,dst=/cache/offline_components,readonly"
  fi
  # Explicit cache mode is the only path that disables the component manager.
  # Without it the normal locked managed-component build is used.
  if [ "${PET_OFFLINE_CACHE:-0}" = 1 ]; then
    printf '%s\n' 'Use --offline-cache rather than setting private runtime flags' >&2; exit 2
  fi
  if [ "$offline" = 1 ]; then
    set -- "$@" --env PET_OFFLINE_CACHE=1 --env IDF_COMPONENT_MANAGER=0
  fi
  mkdir -p "$app/build"
  cid=$("$@" "$image" sh scripts/build.sh --inside-sdk)
  printf '%s\n' "$cid" > "$app/build/tool-container.id"
  printf 'Build container: %s\n' "$cid"
  cleanup() {
    docker rm -f "$cid" >/dev/null
    if docker inspect "$cid" >/dev/null 2>&1; then
      printf 'Container cleanup unverified: %s\n' "$cid" >&2; return 1
    fi
  }
  trap cleanup EXIT
  trap 'exit 130' INT TERM
  docker start --attach "$cid" || true
  test "$(docker inspect --format '{{.State.Status}}' "$cid")" = exited || {
    printf 'Build container did not finish: %s\n' "$cid" >&2; exit 1
  }
  status=$(docker inspect --format '{{.State.ExitCode}}' "$cid")
  exit "$status"
fi
cd "$app"
test "$(idf.py --version)" = "ESP-IDF v5.5.4" || {
  printf '%s\n' 'Requires the pinned ESP-IDF v5.5.4 runtime' >&2; exit 1;
}
python3 - <<'PY'
import hashlib
from pathlib import Path
pins={
 "dependencies.lock":"ee9197346a7f843fc9b0e913280b9d15d8a3dec7497eb85239124433179ab2b1",
 "main/idf_component.yml":"3900352b0660ba70c8cf913aa911faf1666eb82c670243bdf13bfa086ed057b1",
 "sdkconfig.defaults":"b2304dcd8a6b139208129966b9ecfb8a43dc2e1208a863e8ac14c4c63e97e5de",
}
for name,expected in pins.items():
    if hashlib.sha256(Path(name).read_bytes()).hexdigest()!=expected:
        raise SystemExit(f"Dependency/configuration drift: {name}")
PY
if [ "${PET_OFFLINE_CACHE:-0}" = 1 ]; then
  python3 scripts/offline_cache.py /cache/offline_components
fi
assets_python=${PET_ASSET_PYTHON:-/opt/pet-assets/bin/python}
"$assets_python" -c 'import PIL; assert PIL.__version__ == "11.3.0", "Requires Pillow 11.3.0"'
"$assets_python" scripts/export_pet.py
build="$app/build"
config="$app/sdkconfig"
defaults="$app/sdkconfig.defaults"
if [ "$diag" = 1 ]; then
  build="$app/build-bench"
  config="$build/sdkconfig"  # inside the ignored build directory
  defaults="$defaults;$app/sdkconfig.bench.defaults"
fi
if [ "${PET_OFFLINE_CACHE:-0}" = 1 ]; then
  idf.py -B "$build" -D "SDKCONFIG=$config" \
    -D "PET_PRIVATE_MIC_DIAG=$diag" \
    -D "SDKCONFIG_DEFAULTS=$defaults" \
    -D "EXTRA_COMPONENT_DIRS=/cache/offline_components;$app/scripts/offline-components" \
    -D "CMAKE_PROJECT_INCLUDE=$app/scripts/offline-deps.cmake" \
    -D IDF_TARGET=esp32s3 reconfigure
fi
idf.py -B "$build" -D "SDKCONFIG=$config" -D "PET_PRIVATE_MIC_DIAG=$diag" build
# A generated sdkconfig left by an earlier build takes precedence over changed
# sdkconfig.defaults. Never report an image that silently lacks a checked-in
# setting: remove the stale sdkconfig (and build directory) and build again.
python3 - "$config" "$defaults" <<'PY'
from pathlib import Path
import sys
have=set(Path(sys.argv[1]).read_text(encoding="utf-8").splitlines())
missing=[]
for line in "\n".join(Path(p).read_text(encoding="utf-8") for p in sys.argv[2].split(";")).splitlines():
    if not line.startswith("CONFIG_"):
        continue
    key,value=line.split("=",1)
    applied=(not any(h.startswith(key+"=") for h in have)) if value=="n" else line in have
    if not applied:
        missing.append(line)
if missing:
    raise SystemExit("Stale sdkconfig, image does NOT contain: "+", ".join(missing))
PY
if [ "$diag" = 0 ]; then
  # Check the complete linked image too: the native fixture does not compile
  # the USB console dispatcher, and compiler string merging can hide boundaries.
  python3 - "$build/unified_pet.bin" <<'PY'
from pathlib import Path
import sys
data=Path(sys.argv[1]).read_bytes()
markers=("pet-bench-v1","pet-mic-v1","voice on","voice core","voice prio",
         "voice jitter","voice bw","voice audio","voice last","voice scan","kept-end",
         "bv_keep","bridge_voice_bench_last")
found=[marker for marker in markers if marker.encode() in data]
if found:
    raise SystemExit("Private markers in normal binary: "+", ".join(found))
print("NORMAL BINARY PRIVATE MARKER CHECK: PASS")
PY
fi
