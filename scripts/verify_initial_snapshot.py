"""Read-only verification of the frozen initial sources and release bundle.

Later intentional source changes will differ from the initial manifest; this
is a snapshot recovery check, not a permanent CI rule for ongoing development.
"""
from pathlib import Path
import hashlib
import json
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    manifest = json.loads((ROOT/'provenance/initial_snapshot_20261122.json').read_text())
    checked = 0
    for relative, expected in manifest['source_sha256'].items():
        path = ROOT/relative
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise SystemExit('Initial source mismatch: '+relative)
        checked += 1
    release = ROOT/manifest['release_path']
    recorded = json.loads((release/'manifest.json').read_text())
    for name, expected in recorded['archives'].items():
        if hashlib.sha256((release/name).read_bytes()).hexdigest() != expected:
            raise SystemExit('Release archive mismatch: '+name)
    # The optional gem5 submodule may be uninitialized in a lightweight clone.
    gem5 = ROOT/'gem5-src'
    if (gem5/'.git').exists():
        head = subprocess.check_output(['git','-C',str(gem5),'rev-parse','HEAD'],text=True).strip()
        if head != manifest['upstreams']['gem5']['commit']:
            raise SystemExit('gem5 differs from initial pinned commit')
    print(json.dumps({'source_files_matched':checked,'release_archives_matched':len(recorded['archives']),
                      'release_clean_build_recorded':recorded['clean_build_passed']},indent=2))


if __name__ == '__main__':
    main()
