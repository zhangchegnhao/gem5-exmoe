#!/usr/bin/env bash
set -euo pipefail
experiment_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
workspace_root=$(dirname -- "$experiment_root")
cpu=${1:-timing}
case "$cpu" in
    timing|atomic|o3) ;;
    *) echo "Usage: $0 [timing|atomic|o3]" >&2; exit 2 ;;
esac
simulator="$workspace_root/gem5-src/build/X86/gem5.opt"
if [[ ! -x "$simulator" ]]; then
    echo "gem5 is not built yet: $simulator" >&2
    exit 1
fi
"$experiment_root/scripts/build_workloads.sh"
run_dir="$experiment_root/results/$(date +%Y%m%d-%H%M%S)-$cpu-$$"
mkdir -p "$run_dir"
for workload in hello matmul; do
    workload_args=()
    if [[ "$workload" == matmul ]]; then workload_args=(32); fi
    "$experiment_root/bin/$workload" "${workload_args[@]}" \
        > "$run_dir/$workload.native.txt"
    "$simulator" --outdir="$run_dir/$workload" \
        "$experiment_root/configs/x86_se.py" --cpu "$cpu" \
        "$experiment_root/bin/$workload" "${workload_args[@]}" \
        2>&1 | tee "$run_dir/$workload.console.txt"
    # Confirm the simulated program reports the same result as native execution.
    grep -Fxf "$run_dir/$workload.native.txt" "$run_dir/$workload.console.txt"
    python3 "$experiment_root/scripts/summarize.py" "$run_dir/$workload/stats.txt"
done
echo "Verified results: $run_dir"
