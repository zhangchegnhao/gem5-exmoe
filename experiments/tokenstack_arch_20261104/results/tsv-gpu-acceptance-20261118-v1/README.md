# TSV 修正与 A100a 五栈系统验收

日期：2026-11-18。目标是先修正 TSV 聚合吞吐，再接入显式单卡 A100a 计算配置、五栈 TokenStack 和统一计时。所有指标是工程模型输出，不是实际 GPU/HBM 测量。

## 环境与修改

工作区根目录不是 Git 仓库。AttAcc HEAD `c60005143a6b492d7ef83231723386478b59a506`；其 Ramulator base HEAD `b7c70275f04126c647edb989270cc429776955d1`。用户已有 AttAcc/PIM/TokenStack 修改保留，完整差异随离线包归档。gem5-src 保持 clean、HEAD `b9da2bfe1e21f6efa2730edd4e80e58c5d965458`，未修改 gem5 环境。编译使用 GNU C++12、C++20、Release；Python3.10/PyYAML。原始 Git 状态、命令和文件 hash 见 `metadata.json`。

修改/新增的源码：

- Ramulator `src/memory_system/impl/tokenstack_fabric.cpp`：TSV 共享速率日历，保留 PC burst/方向 guard；每源可配置宽准入；全卡 GPU compute 互斥资源。
- Ramulator `src/frontend/impl/memory_trace/tokenstack_graph.cpp`：同龄任务准入轮转，避免低编号任务持续填满源队列。
- `config/tokenstack_hardware.yaml`：显式 TSV 模型和准入宽度；`throughput.py`、`scripts/run_throughput.py`、`tests/test_tsv_bandwidth.py`：独立资源审计和饱和验收。
- `config/tokenstack_a100a.yaml`、`gpu_system.py`、`traces/a100a_decode_micro.yaml`、`scripts/run_gpu_system.py`、`tests/test_gpu_system.py`：GPU 配置、容量分配、计算/访存 DAG、系统计时与守恒验收。
- `scripts/run_hardware.py`、`run_acceptance.py`：共享速率/占用并集审计、统一验收、源码快照；`release_hardware.py`、`reproduce_release.py`：包含 AttAcc Python 来源和统一离线验收。
- `HARDWARE.md`、`GPU_SYSTEM.md`、`README.md`：模型、参数来源、单位和复现说明。

## 执行命令

工作目录 `/home/ubuntu22/gem5`：

```bash
cmake --build attacc_simulator/ramulator2/build -j2
PYTHONDONTWRITEBYTECODE=1 python3 experiments/tokenstack_arch_20261104/scripts/run_acceptance.py tsv-gpu-acceptance-20261118-v1
PYTHONDONTWRITEBYTECODE=1 python3 experiments/tokenstack_arch_20261104/scripts/release_hardware.py tsv-gpu-release-20261118-v1 --clean-build
```

统一验收通过 62 项测试、15 个原硬件场景、8 个 TSV 场景、6 个 GPU 场景。AttAcc bank/BG/buffer 的全部原统计保持一致，周期分别为 273/822/2094。离线发布状态独立读取相邻 `tsv-gpu-release-20261118-v1/manifest.json` 的 `clean_build_passed` 字段；本目录的通过不代替离线发布验收。

离线发布现已完成：150 个 Ramulator 源文件逐一匹配，全新离线构建与统一验收全部通过，`clean_build_passed=true`。归档运行再次通过 62 项测试；8 个吞吐和 6 个 GPU 场景的汇总结果与工作区运行逐项相等。完整补丁、源码、依赖、许可证、复现脚本与 clean 日志均位于相邻发布目录。

## TSV 结果

所有场景均从共同 issue=0 到模拟器停止计算有效带宽，包含启动、refresh 和排空。饱和源队列深度为 256（系统默认仍为 8），每源每 cycle 最多 dispatch 16。

| 场景 | 请求数 | 每栈实际 GB/s |
| --- | ---: | ---: |
| 1 Channel / 896 GB/s TSV | 512 | 38.114 |
| 4 Channel / 896 GB/s TSV | 2048 | 152.183 |
| 16 Channel / 896 GB/s TSV | 8192 | 602.278 |
| 16 Channel / 448 GB/s TSV | 8192 | 335.191 |
| 16 Channel / 112 GB/s TSV | 8192 | 97.314 |
| 16 Channel / 16 GB/s TSV | 8192 | 12.633 |
| 4 Die 共享 112 GB/s TSV | 8192 | 100.232 |
| 2 Stack，各自 112 GB/s TSV | 16384 | 各 97.314 |

旧模型每 32 B burst 独占全栈，理想上界只有 20.806 GB/s。修正后的上界由继承普通 DRAM PC 时序决定，为 665.800 GB/s；不伪称达到 896 GB/s。初次尝试只修改 TSV、未修正任务准入偏置，失败记录保留于 `tsv-throughput-20261118-v1`。正式数值、独立审计及全部日志见相邻 `tsv-gpu-acceptance-20261118-v1-throughput/`。

## 系统结果与守恒

A100a：单卡、108 SM、312 TFLOP/s FP16、utilization 0.8。内存为 40 GiB Capacity + 20 GiB Compute；每栈 TSV 896、UCIe 512 GB/s，全卡 crossbar 2560 GB/s。GPU 访存路径理论上界 2560 GB/s，实际使用回调计时。总容量、原 AttAcc 参数覆盖和来源 hash 见相邻 GPU 目录的 `system_contract.json`。

小型流水包含 GPU query projection、五栈 PIM attention、GPU output projection，同时 promotion 和后台 demotion：

| 场景 | 前台 latency / cycles | 后台完成 / cycles |
| --- | ---: | ---: |
| 默认 | 791 | 336 |
| Compute utilization 降至 0.0008 | 857 | 336 |
| TSV 降至 1 GB/s | 13340 | 7263 |
| UCIe 降至 1 GB/s | 7520 | 336 |
| Crossbar 降至 1 GB/s | 34350 | 336 |
| 后台在 cycle 10000 发出 | 551 | 10800 |

默认前台 791 × 769 ps = 608279 ps = 0.608279 μs。后台提前完成，因此不再叠加；模拟器停止在 792 cycle，另记录 1-cycle 排空差值。两次极小 GPU kernel 共 2 cycles，未计启动成本，不能将这个微型流水时间作为真实 LLM TPOT。

| 来源 | 请求数 | 物理读 B | 物理写 B |
| --- | ---: | ---: | ---: |
| GPU | 810 | 25760 | 160 |
| Promotion | 240 | 1280 | 6400 |
| Demotion | 360 | 9600 | 1920 |
| PIM | 125 | 不计 Bank 内部流量 | 不计 Bank 内部流量 |

总计 1535 请求、36640 B 物理读、8480 B 物理写；每个 RD/WR 对应一条实际 DRAM 命令、一个 data-ready/完成事件及一次 TSV burst 计费。UCIe 和 crossbar 各传输 26240 B GPU 有效 payload，包括 query/output；coordinator 内部流量另记。FP16 等价 migration payload 与 mask-valid/物理 burst 字节分开报告。前台时延只从终端回调计算，GPU 计算不再收取分析式 DRAM 时间。

## 范围与后续

TSV 是周期边界共享流体速率模型，GPU 是算力服务时间模型；没有 GPU 指令级、cache、kernel 内 overlap 或实机启动延迟校准。五头/单 query 流水只是接口验证，不是完整 MoE/LLM。论文 Q1–Q5 仍 open；必须继续区分工程通过与论文忠实度。

`git diff --check` 仍报告历史 HBM3/DRAM lambda/scheduler 文件的尾随空白，本轮未修改这些文件。构建还保留原 DummyMemorySystem 缺少 return 的警告；该实现不在此运行路径。后续最小工作是校准 GPU kernel 参数并建立 LLMSimulator 原生 MoE baseline，再接入现有文件/Trace 接口。
