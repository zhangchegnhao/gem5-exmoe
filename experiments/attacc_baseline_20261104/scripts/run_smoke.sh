#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
experiment_root=$(cd -- "$script_dir/.." && pwd)
workspace_root=$(cd -- "$experiment_root/../.." && pwd)
ramulator_root="$workspace_root/attacc_simulator/ramulator2"
python="$workspace_root/.venvs/attacc/bin/python"
run_id=${1:?usage: run_smoke.sh RUN_ID [DHEAD [NHEAD [SEQLEN [MAXLEN]]]]}
dhead=${2:-128}
nhead=${3:-1}
seqlen=${4:-128}
maxlen=${5:-128}
run_dir="$experiment_root/results/$run_id"

if [[ ! -x "$ramulator_root/build/ramulator2" ]]; then
    echo "Ramulator2 executable missing; configure and prepare it first." >&2
    exit 1
fi
if [[ ! -x "$python" ]]; then
    echo "AttAcc Python environment missing: $python" >&2
    exit 1
fi
mkdir "$run_dir"
mkdir "$run_dir/config" "$run_dir/traces" "$run_dir/logs" "$run_dir/stats"

{
    printf 'run_id=%s\n' "$run_id"
    printf 'attacc_commit=%s\n' "$(git -C "$workspace_root/attacc_simulator" rev-parse HEAD)"
    printf 'ramulator_commit=%s\n' "$(git -C "$ramulator_root" rev-parse HEAD)"
    printf 'dhead=%s\nnhead=%s\nseqlen=%s\nmaxlen=%s\ndbyte=2\n' \
        "$dhead" "$nhead" "$seqlen" "$maxlen"
    printf 'command='; printf '%q ' "$0" "$run_id" "$dhead" "$nhead" "$seqlen" "$maxlen"; printf '\n'
} > "$run_dir/run-metadata.txt"

cmake --build "$ramulator_root/build" -j2 2>&1 | tee "$run_dir/logs/build.log"

for mode in bank bg buffer; do
    "$python" "$ramulator_root/trace_gen/gen_trace_attacc_${mode}.py" \
        --dhead "$dhead" --nhead "$nhead" --seqlen "$seqlen" \
        --maxlen "$maxlen" --output "$run_dir/traces/attacc_${mode}.trace" \
        > "$run_dir/logs/gen-${mode}.log"
done

"$python" - "$ramulator_root" "$run_dir" <<'PY'
from pathlib import Path
import sys
import yaml

ramulator_root, run_dir = map(Path, sys.argv[1:])
for mode in ("bank", "bg", "buffer"):
    config = yaml.safe_load((ramulator_root / f"attacc_{mode}.yaml").read_text())
    config["Frontend"]["path"] = str(run_dir / "traces" / f"attacc_{mode}.trace")
    config["MemorySystem"]["Controller"]["plugins"][0]["ControllerPlugin"]["path"] = str(
        run_dir / "logs" / f"{mode}.cmd.log"
    )
    (run_dir / "config" / f"attacc_{mode}.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False)
    )
PY

for mode in bank bg buffer; do
    "$ramulator_root/build/ramulator2" -f "$run_dir/config/attacc_${mode}.yaml" \
        > "$run_dir/stats/${mode}.stdout.log" 2>&1
done

"$python" - "$run_dir" <<'PY'
from pathlib import Path
import re
import sys

run_dir = Path(sys.argv[1])
rows = ["mode,trace_commands,read_requests,write_requests,pim_mac_all_bank,pim_mac_same_bank,pim_mac_per_bank,memory_system_cycles"]
for mode in ("bank", "bg", "buffer"):
    output = (run_dir / "stats" / f"{mode}.stdout.log").read_text()
    trace_commands = sum(1 for _ in (run_dir / "traces" / f"attacc_{mode}.trace").open())
    fields = {
        key: re.search(rf"^  {re.escape(key)}: (\d+)$", output, re.MULTILINE)
        for key in (
            "total_num_read_requests",
            "total_num_write_requests",
            "total_num_pim_mac_all_bank_requests",
            "total_num_pim_mac_same_bank_requests",
            "total_num_pim_mac_per_bank_requests",
            "memory_system_cycles",
        )
    }
    if any(value is None for value in fields.values()):
        raise SystemExit(f"Missing expected statistics for {mode}: {run_dir}/stats/{mode}.stdout.log")
    rows.append(
        ",".join(
            [mode, str(trace_commands)] + [value.group(1) for value in fields.values()]
        )
    )
(run_dir / "summary.csv").write_text("\n".join(rows) + "\n")
PY

printf 'AttAcc smoke run completed: %s\n' "$run_dir"
