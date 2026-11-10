#!/bin/sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"
docker compose run --rm idf /opt/pet-assets/bin/python scripts/export_pet.py
docker compose run --rm idf idf.py -B build-amoled build
