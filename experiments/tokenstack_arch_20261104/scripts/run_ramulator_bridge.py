#!/usr/bin/env python3
"""Ramulator2 request-retirement trace -> conservative single-channel replay.

Retire means final command issued, or barrier removed, not compute completion.
The replay preserves elapsed intervals (including DRAM waits) exactly once.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import yaml

from ramulator_bridge import load_retirements, replay_requests
from tokenstack_model import LayerKind, MigrationSpec, ResourceArbiter, load_topology


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", help="new results directory name; refuses overwrite")
    parser.add_argument("--trace", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        parser.error("run_id must contain only letters, digits, _ or -")
    root = Path(__file__).resolve().parents[1]
    workspace = root.parents[1]
    ramulator = workspace / "attacc_simulator" / "ramulator2"
    binary = ramulator / "build" / "ramulator2"
    trace = (args.trace or root / "traces" / "attacc_pim_small.trace").resolve()
    result_dir = root / "results" / args.run_id
    result_dir.mkdir(parents=True, exist_ok=False)
    for folder in ("config", "logs", "stats", "traces", "sources"):
        (result_dir / folder).mkdir()
    shutil.copy2(trace, result_dir / "traces" / "input.trace")
    config = yaml.safe_load((ramulator / "attacc_bank.yaml").read_text())
    config["Frontend"]["path"] = str(result_dir / "traces" / "input.trace")
    config["Frontend"]["completion_path"] = str(result_dir / "logs" / "retirement.csv")
    config["MemorySystem"]["Controller"]["plugins"][0]["ControllerPlugin"]["path"] = str(
        result_dir / "logs" / "commands.log")
    effective_config = result_dir / "config" / "attacc_bank.yaml"
    effective_config.write_text(yaml.safe_dump(config, sort_keys=False))
    shutil.copy2(root / "config" / "tokenstack_minimal.yaml", result_dir / "config")

    changed_sources = (
        "src/frontend/impl/memory_trace/pim_loadstore_trace.cpp",
        "src/dram_controller/impl/hbm3_pim_controller.cpp",
        "src/dram_controller/impl/scheduler/pim_scheduler.cpp",
    )
    for relative in changed_sources:
        shutil.copy2(ramulator / relative, result_dir / "sources" / Path(relative).name)
    for source in (root / "tokenstack_model.py", root / "ramulator_bridge.py", Path(__file__)):
        shutil.copy2(source, result_dir / "sources" / source.name)
    command = [str(binary), "-f", str(effective_config)]
    metadata = {
        "command": command, "argv": sys.argv,
        "ramulator_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ramulator, text=True).strip(),
        "attacc_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ramulator.parent, text=True).strip(),
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "trace_sha256": hashlib.sha256(trace.read_bytes()).hexdigest(),
        "python_version": sys.version,
    }
    (result_dir / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    process = subprocess.run(command, cwd=ramulator, text=True, capture_output=True, timeout=60)
    (result_dir / "stats" / "ramulator.stdout.log").write_text(process.stdout)
    (result_dir / "stats" / "ramulator.stderr.log").write_text(process.stderr)
    process.check_returncode()
    match = re.search(r"^  memory_system_cycles: (\d+)$", process.stdout, re.MULTILINE)
    if match is None:
        raise RuntimeError("missing memory_system_cycles")
    cycles = int(match.group(1))
    rows = load_retirements(result_dir / "logs" / "retirement.csv", trace)
    stack = load_topology(str(root / "config" / "tokenstack_minimal.yaml"))
    # All replay and DMA cycles use the actual Ramulator2 DRAM clock.
    stack.clock_hz = 1_000_000_000_000 / rows[0]["tck_ps"]
    requests = replay_requests(rows, stack)
    baseline_arbiter = ResourceArbiter(stack)
    baseline = baseline_arbiter.run(requests, [])
    if [r.complete_cycle for r in baseline] != [r["retire_cycle"] for r in rows]:
        raise RuntimeError("no-migration replay differs from Ramulator2 retirement timeline")
    if baseline_arbiter.stats.last_complete_cycle != cycles:
        raise RuntimeError("PIM retirement horizon does not equal simulator stop cycle")

    channel = requests[0].physical.channel
    capacity = stack.map_address(0x1000, LayerKind.CAPACITY)
    compute = requests[0].physical
    # Mapping of migration endpoints explicitly matches the callback channel.
    from dataclasses import replace
    capacity = replace(capacity, channel=channel)
    migrations = [
        MigrationSpec("promotion-0", capacity, compute, 8192, 0, 0),
        MigrationSpec("demotion-0", compute, capacity, 8192, 10, 1),
    ]
    arbiter = ResourceArbiter(stack)
    results = arbiter.run(requests, migrations)
    checks = arbiter.conservation(results)
    checks["request_ids_conserved"] = sorted(r.request_id for r in results) == sorted(
        [r.request_id for r in requests] + [m.request_id for m in migrations])
    checks["physical_bytes_match_inputs"] = (
        arbiter.stats.migration_physical_read_bytes == 11264
        and arbiter.stats.migration_physical_write_bytes == 11264)
    checks["replay_horizon_conserved"] = arbiter.stats.last_complete_cycle == (
        cycles + sum(r.complete_cycle - r.start_cycle for r in results if r.request_type == "migration"))
    if not all(checks[name] for name in (
        "request_ids_conserved", "physical_bytes_match_inputs", "replay_horizon_conserved",
        "logical_migration_bytes_conserved", "completion_cycle_matches_max_event")):
        raise RuntimeError(f"bridge conservation check failed: {checks}")
    summary = {
        "schema_version": 1,
        "coupling": "offline single-channel conservative interval replay, no feedback to Ramulator2",
        "retire_semantics": "PIM final command issued or barrier removed; not arithmetic completion",
        "tck_ps": rows[0]["tck_ps"],
        "ramulator_stop_cycle": cycles,
        "retired_requests": len(rows),
        "baseline_replay_matches_all_retirement_cycles": True,
        "baseline_replay_horizon_cycle": baseline_arbiter.stats.last_complete_cycle,
        "replay_with_migration_horizon_cycle": arbiter.stats.last_complete_cycle,
        "migration_bandwidth_GBps": stack.tsv_dma_bandwidth_GBps,
        "arbiter_stats": arbiter.stats.as_dict(), "conservation": checks,
    }
    (result_dir / "bridge_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    for name, events in (("baseline_events.csv", baseline), ("arbiter_events.csv", results)):
        with (result_dir / name).open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(asdict(events[0])))
            writer.writeheader()
            writer.writerows(asdict(row) for row in events)
    print(json.dumps(summary, indent=2))
    print(f"Results: {result_dir}")


if __name__ == "__main__":
    main()
