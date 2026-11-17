"""Validate the complete approved offline cache before any asset/build writes.

Pins describe the previously qualified namespace-stripped source snapshot, not
new registry resolution. Algorithm: SHA256 of sorted relative POSIX path, NUL,
file SHA256 hex, newline for every regular file (including metadata/dotfiles).
The package .component_hash must also match the unchanged dependencies.lock.
No source copy, symlink traversal, registry calls or private evidence files.
"""
import hashlib
import json
from pathlib import Path
import sys

import yaml  # Already provided by pinned ESP-IDF; never install at build time.

app = Path(__file__).resolve().parents[1]
cache = Path(sys.argv[1]).expanduser().resolve(strict=True)
pins = json.loads((app / "scripts/offline-cache-pins.json").read_text(encoding="utf-8"))
lock_bytes = (app / "dependencies.lock").read_bytes()
if hashlib.sha256(lock_bytes).hexdigest() != "ee9197346a7f843fc9b0e913280b9d15d8a3dec7497eb85239124433179ab2b1":
    raise SystemExit("Offline lock drift")
lock = yaml.safe_load(lock_bytes)["dependencies"]
packages = {name.split("/")[-1]: data for name, data in lock.items() if name != "idf"}
if set(packages) != set(pins) or {p.name for p in cache.iterdir()} != set(pins):
    raise SystemExit("Offline cache component set differs from pinned lock")
total = 0
for name, pin in sorted(pins.items()):
    folder = cache / name
    if folder.is_symlink() or not folder.is_dir():
        raise SystemExit(f"Offline cache component directory rejected: {name}")
    rows = []
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise SystemExit(f"Offline cache symlink rejected: {name}/{path.name}")
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            rows.append(path.relative_to(folder).as_posix() + "\0" + digest + "\n")
    digest = hashlib.sha256("".join(rows).encode("utf-8")).hexdigest()
    if len(rows) != pin["files"] or digest != pin["tree_sha256"]:
        raise SystemExit(f"Offline cache content drift: {name}")
    if (folder / ".component_hash").read_text(encoding="utf-8").strip() != packages[name]["component_hash"]:
        raise SystemExit(f"Offline package hash differs from lock: {name}")
    print(f"Verified {name} {packages[name]['version']}: {len(rows)} files {digest}")
    total += len(rows)
print(f"Offline cache verified: {len(pins)} locked components, {total} files")