# AttAcc + Ramulator2 baseline

This experiment pins the AttAcc source and its modified Ramulator2 source, builds the simulator, and runs a small HBM3-PIM trace for each AttAcc placement mode. It is the hardware-simulator baseline for later TokenStack work; it does not model Capacity/Compute layers, a Base Die, or migration.

## Pinned sources and environment

- AttAcc simulator: `scale-snu/attacc_simulator`, commit `c60005143a6b492d7ef83231723386478b59a506`.
- Ramulator2 base: commit `b7c70275f04126c647edb989270cc429776955d1`, selected by the AttAcc submodule.
- The AttAcc HBM3/PIM extension is the current Ramulator2 worktree: tracked changes are archived in `patches/ramulator-tracked-attacc.patch`; additional source files and YAMLs are copied from `attacc_simulator/pim_ramulator_src` and recorded in `logs/ramulator-source-status.txt`.
- Python requirements are pinned in `config/requirements.txt`; the prepared venv is `/home/ubuntu22/gem5/.venvs/attacc`.
- CMake configure used GNU C++ 12.3.0. Configure output is in `logs/cmake-configure.log`; build output for the smoke run is in its `logs/build.log`.

The root workspace is not a Git repository. The gem5 source remains in `gem5-src/`; its version and build metadata are recorded in the workspace `README.md` and `gem5-build-info.txt`. No gem5 changes are needed for this baseline.

## Prepare and build

The Ramulator2 submodule already contains the AttAcc patch in this workspace. Do not reset or switch that checkout. The preparation helper is deliberately non-destructive: it verifies the pinned commits and refuses to replace conflicting files. To build from the current prepared state:

```bash
cd /home/ubuntu22/gem5
cmake --build attacc_simulator/ramulator2/build -j2
```

For a new checkout, initialize the AttAcc repository and its Ramulator2 submodule at the pinned commits, then run `scripts/prepare_ramulator.sh`. It will stop if the checkout is on another revision or contains conflicting modifications.

## Run the smoke baseline

Pass a new run ID; the script refuses to reuse an existing result directory. Optional arguments are head dimension, heads per HBM, sequence length, and maximum sequence length.

```bash
cd /home/ubuntu22/gem5
experiments/attacc_baseline_20261104/scripts/run_smoke.sh 20261104-smoke-rerun 128 1 128 128
```

The script builds Ramulator2, generates the three PIM traces, writes run-local YAML configurations and command logs, runs each simulation, and records `summary.csv`. It writes only under `experiments/attacc_baseline_20261104/results/<run-id>/` and the existing Ramulator2 build directory.

## Recorded smoke result

Run: `results/20261104-repro-check/`. Workload parameters: `dhead=128`, `nhead=1`, `seqlen=128`, `maxlen=128`, FP16 (`dbyte=2`). The shipped AttAcc trace generators emit these PIM command counts and Ramulator2 reports:

| Mode | Trace commands | PIM MAC counter | Memory-system cycles |
| --- | ---: | ---: | ---: |
| Bank | 153 | 32 all-bank | 273 |
| Bank group | 345 | 128 same-bank | 822 |
| Buffer | 1273 | 1024 per-bank | 2094 |

The complete simulator stdout, generated traces, effective YAML files, per-channel DRAM command logs, build log, run metadata, and machine-readable `summary.csv` are preserved in the run directory. These cycle counts are outputs of this configured simulator and workload, not measured device latency or a calibrated performance comparison. This smoke run uses a single active address channel in its generated trace; it is a command-path/build check, not a bandwidth study.

## Scope and limits

This reproduces AttAcc's HBM3-PIM command path for its bank, bank-group, and buffer mappings. It does not establish correctness against RTL or silicon, does not validate all address mappings or power assumptions, and does not provide a TokenStack heterogeneous-layer or migration model. Before using larger traces as paper results, record the full workload parameters, source hashes, configuration, and raw logs in a separate run directory.
