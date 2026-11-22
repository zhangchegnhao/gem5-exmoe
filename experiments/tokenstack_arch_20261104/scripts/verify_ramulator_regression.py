#!/usr/bin/env python3
"""Check optional retirement logging against preserved AttAcc smoke baselines."""
import argparse
import csv
import json
from pathlib import Path
import subprocess

import yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    args = parser.parse_args()
    if not args.run_id or Path(args.run_id).name != args.run_id or args.run_id in (".", ".."):
        parser.error("run_id must be a new directory name")
    root = Path(__file__).resolve().parents[1]
    workspace = root.parents[1]
    repo = workspace / "attacc_simulator/ramulator2"
    base = workspace / "experiments/attacc_baseline_20261104/results/20261104-repro-check"
    run = root / "results" / args.run_id
    run.mkdir(exist_ok=False)
    report = []
    for mode in ("bank", "bg", "buffer"):
        trace = base / "traces" / f"attacc_{mode}.trace"
        config = yaml.safe_load((repo / f"attacc_{mode}.yaml").read_text())
        config["Frontend"]["path"] = str(trace)
        output = run / f"{mode}-retirements.csv"
        config["Frontend"]["completion_path"] = str(output)
        config["MemorySystem"]["Controller"]["plugins"][0]["ControllerPlugin"]["path"] = str(run / f"{mode}.cmd")
        path = run / f"{mode}.yaml"
        path.write_text(yaml.safe_dump(config, sort_keys=False))
        command = [str(repo / "build/ramulator2"), "-f", str(path)]
        process = subprocess.run(command, text=True, capture_output=True, timeout=60)
        (run / f"{mode}.stdout.log").write_text(process.stdout)
        (run / f"{mode}.stderr.log").write_text(process.stderr)
        process.check_returncode()
        stats = yaml.safe_load(process.stdout[process.stdout.index("Frontend:"):])["MemorySystem"]
        original = (base / "stats" / f"{mode}.stdout.log").read_text()
        old = yaml.safe_load(original[original.index("Frontend:"):])["MemorySystem"]
        if stats != old:
            raise RuntimeError(f"{mode}: statistics differ from baseline")
        with output.open() as stream:
            rows = list(csv.DictReader(stream))
        source = trace.read_text().splitlines()
        if len(rows) != len(source) or {int(r["source_id"]) for r in rows} != set(range(len(source))):
            raise RuntimeError(f"{mode}: requests dropped or duplicated")
        if max(int(r["retire_cycle"]) for r in rows) != stats["memory_system_cycles"]:
            raise RuntimeError(f"{mode}: retirement horizon differs")
        report.append(dict(mode=mode, command=command, request_count=len(rows),
                           barrier_count=sum(r["event"] == "barrier_removed" for r in rows),
                           cycles=stats["memory_system_cycles"], all_stats_match_baseline=True,
                           all_request_ids_retired_once=True))
    (run / "regression.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
