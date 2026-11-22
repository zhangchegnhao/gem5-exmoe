# Closed-loop KV migration verification

Goal: real AttAcc Ramulator2 RD/WR transactions for promotion/demotion, shared
Channel/Base Die transaction admission, and runtime controller gating for the
additional internal TSV/Base Die link.

Run from `/home/ubuntu22/gem5`:

```bash
cmake --build attacc_simulator/ramulator2/build -j2
PYTHONDONTWRITEBYTECODE=1 python3 experiments/tokenstack_arch_20261104/scripts/run_closed_loop.py NEW_RUN_ID
```

Use a new run ID; existing outputs are never reused. `metadata.json` records
pinned commits, the actual configured g++-12 compiler, and executable/library
hashes. `CMakeCache.txt` and `compile_commands.json` record build settings.
`sources/` contains the driver, controller, capability header, CMake registration,
scheduler, runner, tests, configuration, workload and incremental patches.
`source-sha256.json` identifies these snapshots. Apply the retirement patch then
the closed-loop patch to an AttAcc-prepared baseline; their composition was
verified byte-for-byte in `patch-chain-verification.log`.

`summary.json` reports five actual simulator runs. Every scenario preserves its
effective YAML, workload, exact executable command, stdout/stderr, raw DRAM
commands, per-request events, per-job events, and resource intervals.
`verification.json` records build/tests and the preserved AttAcc baseline
regression location. Full successful test outputs are retained alongside it.

The normal migration case has 716 requests: 12 PIM (including barriers), 352 RD,
352 WR. Physical source reads and destination writes each total 11264 B; two
8192 B logical FP16-equivalent migrations reserve at most 8192 B of Buffer.
Its final event and simulator stop both occur at cycle 4073 (tCK=769 ps).
No-migration ends at cycle 42; reducing the additional link bandwidth from 896
to 28 decimal GB/s moves the migration endpoint to cycle 5087. These outputs
demonstrate feedback and conservation, not hardware performance improvement.

The 3.5 GB/s stress case exercises refresh enqueue/drain during gating. This
does not validate refresh deadlines or retention. Queue depth 1 exposes
admission priority inversion; priority applies among admitted jobs only.

Scope: one Stack/Channel; two non-overlapping logical regions in one physical
DRAM timing domain; nonpreemptive transaction ownership; one DMA; whole-page
Buffer reservation. Internal hops are additional serial transfers beyond the
DRAM bus; their physical placement and overlap need calibration. Quant/dequant
is assumed throughput matched, without metadata. PIM events mean final command
issue or barrier removal, not arithmetic completion. KV contents/numerical
quantization, independent physical Layer banks, multiple Stacks, DMA overlap,
and end-to-end MoE are not modeled.
