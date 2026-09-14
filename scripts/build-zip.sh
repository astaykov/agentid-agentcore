#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stage_dir="$repo_root/build/stage"
output_zip="$repo_root/build/agent-runtime.zip"

rm -rf "$stage_dir" "$output_zip"
mkdir -p "$stage_dir"

python3 -m pip install \
    --requirement "$repo_root/agent/requirements.txt" \
    --target "$stage_dir" \
    --platform manylinux2014_aarch64 \
    --python-version 3.12 \
    --implementation cp \
    --abi cp312 \
    --only-binary=:all: \
    --no-compile \
    --index-url https://pypi.org/simple/

cp "$repo_root"/agent/src/*.py "$stage_dir"/
mv "$stage_dir/agent.py" "$stage_dir/app.py"

find "$stage_dir" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$stage_dir" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete

STAGE_DIR="$stage_dir" OUTPUT_ZIP="$output_zip" python3 -c '
import os
from pathlib import Path
import zipfile

stage = Path(os.environ["STAGE_DIR"])
output = Path(os.environ["OUTPUT_ZIP"])
with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
    for path in stage.rglob("*"):
        if path.is_file():
            archive.write(path, path.relative_to(stage))
'

if python3 -c '
import sys
import zipfile

with zipfile.ZipFile(sys.argv[1]) as archive:
    bad = [
        name for name in archive.namelist()
        if "__pycache__/" in name or name.endswith((".pyc", ".pyo"))
    ]
if bad:
    print("\n".join(bad[:10]))
    raise SystemExit(1)
' "$output_zip"; then
    printf 'Built %s\n' "$output_zip"
else
    printf 'Artifact contains Python cache files\n' >&2
    exit 1
fi
