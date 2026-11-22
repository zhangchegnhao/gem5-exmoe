#!/usr/bin/env bash
set -euo pipefail
experiment_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
mkdir -p "$experiment_root/bin"
for workload in hello matmul; do
    gcc -O2 -std=c11 -Wall -Wextra -static -march=x86-64 -mtune=generic \
        "$experiment_root/workloads/$workload.c" -o "$experiment_root/bin/$workload"
done
file "$experiment_root/bin/hello" "$experiment_root/bin/matmul"
