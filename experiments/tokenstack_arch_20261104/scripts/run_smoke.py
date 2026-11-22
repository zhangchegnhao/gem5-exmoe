#!/usr/bin/env python3
"""Run the minimal TokenStack topology and migration smoke test."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from tokenstack_model import LayerKind, load_topology


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    result_dir = root / "results" / "20261104-topology-smoke"
    result_dir.mkdir(parents=True, exist_ok=False)
    stack = load_topology(str(root / "config" / "tokenstack_minimal.yaml"))

    capacity = stack.map_address(0x1000, LayerKind.CAPACITY)
    compute = stack.map_address(0x1000, LayerKind.COMPUTE)
    promotion = stack.migrate("promotion-0", capacity, compute, 8192, issue_cycle=0, priority=0)
    demotion = stack.migrate("demotion-0", compute, capacity, 8192, issue_cycle=10, priority=1)

    summary = {
        "precision": "behavior-level, shared-DMA cycle approximation",
        "topology": {
            "layers": [layer.__dict__ | {"kind": layer.kind.value} for layer in stack.layers],
            "bytes_per_dma_cycle": stack.bytes_per_cycle,
        },
        "addresses": {"capacity": capacity.__dict__ | {"layer_kind": capacity.layer_kind.value},
                      "compute": compute.__dict__ | {"layer_kind": compute.layer_kind.value}},
        "migrations": [promotion.__dict__, demotion.__dict__],
        "stats": stack.stats.as_dict(),
    }
    (result_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    with (result_dir / "migrations.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(promotion.__dict__))
        writer.writeheader()
        writer.writerows([promotion.__dict__, demotion.__dict__])
    print(f"TokenStack topology smoke completed: {result_dir}")


if __name__ == "__main__":
    main()
