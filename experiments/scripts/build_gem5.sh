#!/usr/bin/env bash
set -euo pipefail
experiment_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$experiment_root/../gem5-src"
exec scons build/X86/gem5.opt -j"${JOBS:-2}" --linker=gold "$@"
