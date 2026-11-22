#!/usr/bin/env bash
# Prepare the pinned AttAcc Ramulator2 extension without resetting a checkout.
set -euo pipefail
experiment_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
workspace_root=$(cd -- "$experiment_root/../.." && pwd)
attacc_root="$workspace_root/attacc_simulator"
ramulator_root="$attacc_root/ramulator2"
expected_attacc=c60005143a6b492d7ef83231723386478b59a506
expected_ramulator=b7c70275f04126c647edb989270cc429776955d1
[[ $(git -C "$attacc_root" rev-parse HEAD) == "$expected_attacc" ]]
if [[ $(git -C "$ramulator_root" rev-parse HEAD) != "$expected_ramulator" ]]; then
    echo "Ramulator checkout is not at the pinned base commit; refusing to switch it." >&2
    exit 1
fi
if ! git -C "$ramulator_root" diff --cached --quiet; then
    echo "Ramulator checkout has staged changes; refusing to modify it." >&2
    exit 1
fi

python3 - "$attacc_root" "$ramulator_root" <<'PY'
from pathlib import Path
import re
import sys

attacc, ramulator = map(Path, sys.argv[1:])

def copy_without_replacement(source: Path, destination: Path) -> None:
    if source.is_dir():
        destination.mkdir(exist_ok=True)
        for child in source.iterdir():
            copy_without_replacement(child, destination / child.name)
    elif destination.exists():
        if not destination.is_file() or destination.read_bytes() != source.read_bytes():
            raise RuntimeError(f'Refusing to replace conflicting file: {destination}')
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())

for line in (attacc / 'set_pim_ramulator.sh').read_text().splitlines():
    match = re.fullmatch(r'cp( -r)? (pim_ramulator_src/\S+) (ramulator2/\S*)', line)
    if not match:
        continue
    source = attacc / match[2]
    destination = attacc / match[3]
    target = destination / source.name
    copy_without_replacement(source, target)
PY
cd "$ramulator_root"
archive_patch="$experiment_root/patches/ramulator-tracked-attacc.patch"
if git diff --quiet; then
    for source_patch in patches/*.patch; do
        patch --batch --forward -p1 < "$source_patch"
    done
else
    if [[ ! -f "$archive_patch" ]] || ! git diff --binary | cmp -s - "$archive_patch"; then
        echo "Ramulator tracked changes differ from the archived AttAcc patch; refusing to continue." >&2
        exit 1
    fi
fi
if [[ ! -e "$archive_patch" ]]; then
    git diff --binary > "$archive_patch"
fi
git diff --binary | cmp - "$archive_patch"
git status --short > "$experiment_root/logs/ramulator-source-status.txt"
echo "Prepared AttAcc $expected_attacc on Ramulator $expected_ramulator"
