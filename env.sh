# Source this file in Bash: source /home/ubuntu22/gem5/env.sh
gem5_workspace_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export GEM5_ROOT="$gem5_workspace_dir/gem5-src"
export GEM5_EXPERIMENTS="$gem5_workspace_dir/experiments"
case ":$PATH:" in
    *":$GEM5_ROOT/build/X86:"*) ;;
    *) export PATH="$GEM5_ROOT/build/X86:$PATH" ;;
esac
unset gem5_workspace_dir
