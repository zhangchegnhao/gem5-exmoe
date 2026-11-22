# Two independent Layer timing domains: reproducible run

Goal: separate Capacity/Compute DRAM/controller state while retaining one shared
Stack Channel/TSV/Base Die resource path. The full model scope, source file list,
parameter units, trace format and reproduction instructions are in the preserved
`sources/LAYERED.md` (also available in the experiment root).

This is a cycle-level AttAcc DRAM command backend plus a conservative behavior
model for shared issue admission. Capacity uses the ordinary RD/WR subset of
HBM3-PIM; PIM requests to Capacity are rejected. Two mapped 1 MiB address windows
are smoke-test regions, not physical Die capacities. Each domain retains the
existing HBM3_8Gb_2R organization. Both clocks are 769 ps; other clock ratios and
multiple Channels are rejected.

From `/home/ubuntu22/gem5`, after preparing the pinned AttAcc baseline and
applying the retirement, closed-loop and layered-domain patches in that order:

```bash
cmake --build attacc_simulator/ramulator2/build -j2
PYTHONDONTWRITEBYTECODE=1 python3 experiments/tokenstack_arch_20261104/scripts/run_layered.py NEW_RUN_ID
```

Use a fresh run ID. `metadata.json`, CMakeCache and compile_commands record the
actual g++-12 build configuration, source revisions and executable/library hashes.
`sources/` snapshots code/config/trace/test inputs and all three patches;
`source-sha256.json` identifies their exact bytes. `verification.json` and the
adjacent logs record 15 passing tests, AttAcc 273/822/2094-cycle regressions, and
the byte-for-byte patch-chain check. No gem5 source/build changes were made.

`summary.json` contains six actual simulator executions. Every scenario retains
the exact command, effective config, workload, raw stdout/stderr, per-domain
DRAM command logs, request/job/resource events, and one complete `grants.csv`.
Layer request events use schema v2 with global address, Layer ID, local address
and tCK; older single-domain runs retain schema v1.

The standard migration workload has 716 accepted requests: Capacity 96 RD/96 WR,
Compute 256 RD/256 WR/12 PIM (including barriers). Two 8192 B logical migrations
read/write 11264 physical bytes each, reserve at most 8192 B of Buffer, and finish
at cycle 4148. All request IDs, burst addresses/bytes, final commands, callbacks,
domain clocks/counters, grants and drain horizons pass the conservation audit.

The isolation workload opens the same local row coordinates independently on
both domains, checks local row hits/conflicts, and verifies that Compute PIM
PREA/ACTAB does not close Capacity's open row. A separate timing perturbation
increases Capacity nCL only: its RD return latency becomes 28 cycles, while
Compute remains at 21 cycles. Queuing and dependencies still propagate delay.

Limitations: one Channel, one DMA, nonpreemptive whole-page transactions; the
64-cycle domain switch guard is a synthetic conservative setting, not calibrated
TokenStack hardware. Internal links are additional serial hops; conversion is
assumed throughput matched without metadata. Refresh enqueue/drain is tested,
not retention/deadline compliance. PIM callbacks mean final command issue or
barrier removal, not arithmetic result completion. Physical Die geometry,
overlapping/chunked DMA, numerical KV data, multiple Stacks and end-to-end MoE
remain future work. No performance improvement is claimed from these checks.
