#!/usr/bin/env python3
"""Run AttAcc PIM and TokenStack migration events through one arbiter."""

from __future__ import annotations

import csv
import argparse
import json
from dataclasses import asdict
from pathlib import Path

from tokenstack_model import (
    LayerKind,
    MigrationSpec,
    ResourceArbiter,
    load_attacc_pim_trace,
    load_topology,
)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", help="new run directory name")
    args = parser.parse_args()
    if not args.run_id or Path(args.run_id).name != args.run_id or args.run_id in (".", ".."):
        parser.error("run_id must be a single directory name")
    result_dir = root / "results" / args.run_id
    result_dir.mkdir(parents=True, exist_ok=False)
    stack = load_topology(str(root / "config" / "tokenstack_minimal.yaml"))
    trace = root / "traces" / "attacc_pim_small.trace"
    pim_requests = load_attacc_pim_trace(trace, stack)

    capacity = stack.map_address(0x1000, LayerKind.CAPACITY)
    compute = stack.map_address(0x1000, LayerKind.COMPUTE)
    migrations = [
        MigrationSpec("promotion-0", capacity, compute, 8192, issue_cycle=0, priority=0),
        MigrationSpec("demotion-0", compute, capacity, 8192, issue_cycle=10, priority=1),
    ]

    arbiter = ResourceArbiter(stack)
    results = arbiter.run(pim_requests, migrations)
    conservation = arbiter.conservation(results)
    assert conservation["logical_migration_bytes_conserved"]
    assert conservation["completion_cycle_matches_max_event"]
    assert conservation["pim_request_count"] == len(pim_requests)
    assert arbiter.stats.migration_physical_write_bytes == 3072 + 8192
    assert results[0].request_type == "pim"
    assert any(row.request_type == "pim" and row.queue_cycles > 0 for row in results)

    summary = {
        "precision": "behavior-level, non-preemptive shared Channel/Base Die/TSV arbitration",
        "trace": str(trace),
        "pim_requests": len(pim_requests),
        "migrations": [asdict(request) for request in migrations],
        "events": [asdict(row) for row in results],
        "arbiter_stats": arbiter.stats.as_dict(),
        "conservation": conservation,
    }
    (result_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
    with (result_dir / "events.csv").open("w", newline="") as stream:
        fields = list(asdict(results[0]))
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(asdict(row) for row in results)
    print(f"TokenStack contention smoke completed: {result_dir}")


if __name__ == "__main__":
    main()
