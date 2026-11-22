# 工程版本管理

首次纳管：2026-11-22。远端为 https://github.com/zhangchegnhao/gem5-exmoe.git。

## 管理范围

- `attacc_simulator/` 和其中修改版 `ramulator2/` 的源码、配置、许可证直接由根仓库跟踪。Ramulator 的已有 tracked 和 untracked 扩展都包含在快照中；不是只记录上游 submodule commit。
- 原工作区内的嵌套 `.git` 元数据保持原样，未加入根仓库。请在工程根目录执行项目级 `git status/add/diff/commit`，避免误提交到原上游仓库。新 clone 只有根仓库，无此历史嵌套结构。
- `gem5-src` 无本地修改，以固定上游 commit 的子模块管理。首次纳管不移动源码、不重新编译、不改变已有 gem5 构建。
- `experiments/` 的源码、配置、输入 Trace、测试、文档和历史顶层摘要均纳管。最新 `tsv-gpu-acceptance-20261118-v1{,-attacc,-throughput,-gpu}` 的原始 CSV/命令日志完整纳管；重复 `sources/` 快照排除。
- `tsv-gpu-release-20261118-v1` 的离线包、manifest、完整补丁、构建/测试日志和复现脚本纳管。旧完整输出和重复 clean-run 仍在原工作区，未全部提交，不能假定历史摘要引用的每个文件都在新 clone 内。
- 编译产物、依赖展开目录、虚拟环境、凭据目录、编辑器状态不提交。上游 commit、依赖版本和首次纳管时的源码 SHA256 见 `provenance/initial_snapshot_20261122.json`。

## 获取与复现

```bash
git clone https://github.com/zhangchegnhao/gem5-exmoe.git
cd gem5-exmoe
```

当前 TokenStack 路径不需要 gem5 编译产物。如需 gem5 源码：

```bash
git submodule update --init gem5-src
```

建议用已通过验收的离线包，在**不存在的新目录**重建。需要 Python3.10+、PyYAML、CMake、GNU C++12 和兼容 Linux 环境：

```bash
python3 -m venv .venvs/tokenstack
.venvs/tokenstack/bin/pip install -r requirements.txt
source .venvs/tokenstack/bin/activate
python3 experiments/tokenstack_arch_20261104/results/tsv-gpu-release-20261118-v1/reproduce_release.py \
  experiments/tokenstack_arch_20261104/results/tsv-gpu-release-20261118-v1 \
  /absolute/path/to/new-reproduction-workspace
```

历史配置和日志保留采集时的绝对路径用于审计；不要直接运行其旧 `config.yaml`。当前脚本与离线复现脚本会根据新的路径重新生成配置。

若要在**新的 clone** 中直接编译当前纳管源码，可使用同一离线包提供的固定依赖。下面的 `ext` 目录必须尚不存在，避免覆盖已有开发环境：

```bash
mkdir attacc_simulator/ramulator2/ext
tar -xzf experiments/tokenstack_arch_20261104/results/tsv-gpu-release-20261118-v1/dependencies.tar.gz \
  -C attacc_simulator/ramulator2/ext
cmake -S attacc_simulator/ramulator2 -B attacc_simulator/ramulator2/build \
  -DCMAKE_CXX_COMPILER=/usr/bin/g++-12 -DCMAKE_BUILD_TYPE=Release \
  -DFETCHCONTENT_FULLY_DISCONNECTED=ON \
  -DFETCHCONTENT_SOURCE_DIR_YAML-CPP="$PWD/attacc_simulator/ramulator2/ext/yaml-cpp" \
  -DFETCHCONTENT_SOURCE_DIR_SPDLOG="$PWD/attacc_simulator/ramulator2/ext/spdlog" \
  -DFETCHCONTENT_SOURCE_DIR_ARGPARSE="$PWD/attacc_simulator/ramulator2/ext/argparse"
cmake --build attacc_simulator/ramulator2/build -j2
PYTHONDONTWRITEBYTECODE=1 python3 experiments/tokenstack_arch_20261104/scripts/run_acceptance.py my-new-run
```

源码快照恢复检查：`python3 scripts/verify_initial_snapshot.py`。它核对的是首次快照；后续有意修改源码后出现 hash 不一致是正常的，不要篡改初始清单来掩盖变化。首次 Git 导出构建与验收日志见 `provenance/initial_git_validation/`。

## 日常开发

在根目录修改、提交实际源码即可，不要重新运行历史 `set_pim_ramulator.sh` 或 `prepare_ramulator.sh` 覆盖当前扩展。

```bash
git status --short
git diff
git add attacc_simulator/ramulator2/src experiments/tokenstack_arch_20261104
git diff --cached --stat
git commit -m "Describe the validated change"
git push origin main
```

新结果目录默认只纳管顶层摘要。要固定新一轮原始证据，可在 `.gitignore` 加入精确 run ID 的白名单，保留输入/config/日志/统计，并继续排除重复源码快照与二进制。

首次快照的离线包是不可变历史证据，不代表以后源码自动更新了发布包。历史 `release_hardware.py` 依赖独立的 AttAcc/Ramulator Git 上游历史；在新 clone 中不应运行它来生成新包，须先适配为根仓库版本来源。现有离线 `reproduce_release.py` 不受此限制。当前硬件与 GPU 测试可在构建后运行，但未校准的 A100a 性能模型仍不能作为实机 LLM 结果。

## 许可证

保留所有第三方原始许可证：AttAcc 的 `LICENSE`、Ramulator 的 `LICENSE`、离线依赖包内许可证，以及 gem5 子模块许可证。未擅自为项目原创部分新增开源许可；公开仓库本身不等同于对全部内容授予统一许可证。
