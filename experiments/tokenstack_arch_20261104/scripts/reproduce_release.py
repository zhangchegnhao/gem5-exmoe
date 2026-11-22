"""Rebuild a released TokenStack bundle in a NEW workspace, without a Git clone."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('bundle',type=Path);parser.add_argument('workspace',type=Path)
    args=parser.parse_args();bundle=args.bundle.resolve();workspace=args.workspace.resolve()
    manifest=json.loads((bundle/'manifest.json').read_text())
    for name,expected in manifest['archives'].items():
        if sha256(bundle/name)!=expected:raise ValueError('bundle hash mismatch: '+name)
    workspace.mkdir(exist_ok=False);checkout=workspace/'attacc_simulator/ramulator2';checkout.mkdir(parents=True)
    with tarfile.open(bundle/'ramulator-base.tar') as archive:archive.extractall(checkout)
    subprocess.run(['git','apply','--check',str(bundle/'ramulator-complete.patch')],cwd=checkout,check=True)
    subprocess.run(['git','apply',str(bundle/'ramulator-complete.patch')],cwd=checkout,check=True)
    for name,expected in manifest['source_sha256'].items():
        if sha256(checkout/name)!=expected:raise ValueError('source reconstruction mismatch: '+name)
    with tarfile.open(bundle/'dependencies.tar.gz') as archive:archive.extractall(checkout/'ext')
    with tarfile.open(bundle/'tokenstack-tools.tar.gz') as archive:archive.extractall(workspace)
    for name,expected in manifest.get('attacc_compute_source_sha256',{}).items():
        if sha256(workspace/'attacc_simulator'/name)!=expected:raise ValueError('AttAcc compute source mismatch: '+name)
    configure=['cmake','-S',str(checkout),'-B',str(checkout/'build'),'-DCMAKE_CXX_COMPILER=/usr/bin/g++-12',
               '-DCMAKE_BUILD_TYPE=Release','-DFETCHCONTENT_FULLY_DISCONNECTED=ON']
    configure += [f'-DFETCHCONTENT_SOURCE_DIR_{name.upper()}={checkout / "ext" / name}' for name in manifest['dependencies']]
    subprocess.run(configure,check=True);subprocess.run(['cmake','--build',str(checkout/'build'),'-j2'],check=True)
    root=workspace/'experiments/tokenstack_arch_20261104'
    subprocess.run(['python3',str(root/'scripts/run_acceptance.py'),'reproduced-engineering',
                    '--profile','tokenstack_paper_profile','--allow-engineering-assumptions'],cwd=workspace,check=True)
    print('Engineering acceptance passed. Paper fidelity remains blocked as documented.')


if __name__=='__main__':main()
