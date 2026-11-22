"""Archive exact patched sources/dependencies and verify a clean offline build.

No checkout is reset, cleaned, or switched. A fresh git archive is patched in a
temporary directory; original gem5/AttAcc trees are used read-only by this script.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT=Path(__file__).resolve().parents[1]
WORKSPACE=ROOT.parents[1]
REPO=WORKSPACE/"attacc_simulator/ramulator2"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(command: list[str],cwd: Path,log: Path,timeout: int=600) -> subprocess.CompletedProcess:
    with log.open('w') as stream:
        proc=subprocess.run(command,cwd=cwd,text=True,stdout=stream,stderr=subprocess.STDOUT,timeout=timeout,
                            env={**os.environ,"PYTHONDONTWRITEBYTECODE":"1"})
    if proc.returncode:raise RuntimeError(f"command failed ({proc.returncode}); see {log}")
    return proc


def tree_files(root: Path) -> list[Path]:
    return [p for p in root.rglob('*') if p.is_file() and not any(x in p.relative_to(root).parts for x in ('.git','__pycache__','results'))]


def make_release(output: Path,clean_build: bool) -> dict:
    output.mkdir(parents=True,exist_ok=False)
    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO,text=True).strip()
    tracked=subprocess.check_output(['git','diff','--binary','HEAD','--'],cwd=REPO)
    untracked=subprocess.check_output(['git','ls-files','--others','--exclude-standard'],cwd=REPO,text=True).splitlines()
    additions=[name for name in untracked if name.startswith(('src/','trace_gen/')) or ('/' not in name and name.endswith('.yaml'))]
    patch=tracked
    for name in sorted(additions):
        proc=subprocess.run(['git','diff','--no-index','--binary','--','/dev/null',name],cwd=REPO,capture_output=True)
        if proc.returncode not in (0,1):raise RuntimeError(proc.stderr.decode())
        patch+=proc.stdout
    patch_path=output/'ramulator-complete.patch';patch_path.write_bytes(patch)
    base=output/'ramulator-base.tar'
    with base.open('wb') as stream:subprocess.run(['git','archive',head],cwd=REPO,stdout=stream,check=True)
    tracked_names=subprocess.check_output(['git','ls-files'],cwd=REPO,text=True).splitlines()
    source_hashes={name:digest(REPO/name) for name in sorted(set(tracked_names+additions)) if (REPO/name).is_file()}
    dependencies={}
    with tarfile.open(output/'dependencies.tar.gz','w:gz') as archive:
        for name in ('yaml-cpp','spdlog','argparse'):
            directory=REPO/'ext'/name
            commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=directory,text=True).strip()
            files=tree_files(directory)
            dependencies[name]={"commit":commit,"sha256":{str(p.relative_to(directory)):digest(p) for p in files}}
            for path in files:archive.add(path,arcname=str(Path(name)/path.relative_to(directory)),recursive=False)
    with tarfile.open(output/'tokenstack-tools.tar.gz','w:gz') as archive:
        paths=list(ROOT.glob('*.py'))+list(ROOT.glob('*.md'))
        for name in ('scripts','tests','config','traces','spec','references','patches'):
            paths+=tree_files(ROOT/name)
        baseline=WORKSPACE/'experiments/attacc_baseline_20261104/results/20261104-repro-check'
        paths+=[p for p in baseline.rglob('*') if p.is_file()]
        paths += [REPO.parent/name for name in ('src/config.py','src/model.py','src/type.py','LICENSE')]
        for path in paths:archive.add(path,arcname=str(path.relative_to(WORKSPACE)),recursive=False)
    metadata={"schema_version":1,"ramulator_base_commit":head,"source_sha256":source_hashes,"dependencies":dependencies,
              "attacc_compute_source_commit":subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO.parent,text=True).strip(),
              "attacc_compute_source_sha256":{name:digest(REPO.parent/name) for name in ('src/config.py','src/model.py','src/type.py','LICENSE')},
              "compiler":"/usr/bin/g++-12","build_type":"Release","CXX_standard":20,
              "clean_build_requested":clean_build,"clean_build_passed":False,
              "paper_spec_frozen":False,"paper_fidelity_acceptance":"blocked_Q1_to_Q5",
              "archives":{name:digest(output/name) for name in ('ramulator-base.tar','ramulator-complete.patch','dependencies.tar.gz','tokenstack-tools.tar.gz')}}
    (output/'manifest.json').write_text(json.dumps(metadata,indent=2)+'\n')
    shutil.copy2(ROOT/'scripts/reproduce_release.py',output/'reproduce_release.py')
    (output/'REPRODUCE.md').write_text('''# Offline reproduction\n\nRequires Python3.10+, PyYAML, CMake and GNU C++12 on a compatible Linux system.\n\nRun `python3 reproduce_release.py /absolute/path/to/this/bundle /absolute/path/to/new-workspace`.\nThe destination MUST NOT exist. Archives and reconstructed sources are hash-checked;\nthen a fresh Release build and the full engineering acceptance matrix run offline.\nDependencies and their licenses are included; no Git clone or network fetch is needed.\n\nThis bundle implements a declared engineering profile. Paper-fidelity acceptance\nremains blocked by Q1-Q5; a successful build does not resolve those questions.\n''')
    if not clean_build:return metadata
    with tempfile.TemporaryDirectory(prefix='tokenstack-clean-release-') as temporary:
        workspace=Path(temporary);checkout=workspace/'attacc_simulator/ramulator2';checkout.mkdir(parents=True)
        with tarfile.open(base) as archive:archive.extractall(checkout)
        run(['git','apply','--check',str(patch_path)],checkout,output/'patch-check.log')
        run(['git','apply',str(patch_path)],checkout,output/'patch-apply.log')
        for name,expected in source_hashes.items():
            if digest(checkout/name)!=expected:raise RuntimeError(f'clean reconstruction differs: {name}')
        with tarfile.open(output/'dependencies.tar.gz') as archive:archive.extractall(checkout/'ext')
        with tarfile.open(output/'tokenstack-tools.tar.gz') as archive:archive.extractall(workspace)
        configure=['cmake','-S',str(checkout),'-B',str(checkout/'build'),'-DCMAKE_CXX_COMPILER=/usr/bin/g++-12',
                   '-DCMAKE_BUILD_TYPE=Release','-DFETCHCONTENT_FULLY_DISCONNECTED=ON']
        configure += [f'-DFETCHCONTENT_SOURCE_DIR_{name.upper()}={checkout / "ext" / name}' for name in dependencies]
        run(configure,workspace,output/'clean-configure.log')
        run(['cmake','--build',str(checkout/'build'),'-j2'],workspace,output/'clean-build.log')
        experiment=workspace/'experiments'/ROOT.name
        run(['python3',str(experiment/'scripts/run_hardware.py'),'clean-smoke'],workspace,output/'clean-smoke.log')
        run(['python3',str(experiment/'scripts/run_acceptance.py'),'clean-engineering'],workspace,output/'clean-acceptance.log')
        shutil.copy2(experiment/'results/clean-engineering/tests.log',output/'clean-tests.log')
        shutil.copytree(experiment/'results',output/'clean-results')
        shutil.copytree(experiment/'results/clean-smoke',output/'clean-smoke-results')
        shutil.copytree(experiment/'results/clean-engineering-attacc',output/'clean-baseline-results')
        metadata.update(clean_build_passed=True,clean_source_files_matched=len(source_hashes),
                        clean_executable_sha256=digest(checkout/'build/ramulator2'),clean_library_sha256=digest(checkout/'libramulator.so'),
                        clean_smoke=json.loads((experiment/'results/clean-smoke/verification.json').read_text()),
                        clean_baseline=json.loads((experiment/'results/clean-engineering-attacc/regression.json').read_text()),
                        clean_engineering_acceptance=json.loads((experiment/'results/clean-engineering/acceptance.json').read_text())['engineering_acceptance'],
                        clean_throughput_acceptance=json.loads((experiment/'results/clean-engineering-throughput/throughput.json').read_text())['acceptance'],
                        clean_gpu_acceptance=json.loads((experiment/'results/clean-engineering-gpu/system_acceptance.json').read_text())['acceptance'])
    (output/'manifest.json').write_text(json.dumps(metadata,indent=2)+'\n');return metadata


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('run_id');parser.add_argument('--clean-build',action='store_true');args=parser.parse_args()
    if Path(args.run_id).name!=args.run_id or args.run_id in ('.','..'):parser.error('new simple run ID required')
    report=make_release(ROOT/'results'/args.run_id,args.clean_build)
    print(json.dumps({key:report[key] for key in ('clean_build_requested','clean_build_passed','paper_spec_frozen','paper_fidelity_acceptance')},indent=2))


if __name__=='__main__':main()
