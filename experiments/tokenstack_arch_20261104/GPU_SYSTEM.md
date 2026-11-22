# A100a 计算参数 + 五栈 TokenStack 系统接口

2026-11-18。这是用户指定的实验组合，不是商用 A100 的原装内存配置，也不是论文性能复现。硬件时序、GPU 服务时间、Trace 数据依赖各自建模。

## 配置与口径

入口 `config/tokenstack_a100a.yaml` 引用 `tokenstack_hardware.yaml`，覆盖栈数为 5。`gpu_system.configure_system()` 实际调用当前 AttAcc `make_xpu_config(GPUType.A100a)`；保留 108 SM、312e12 FP16 FLOP/s，默认计算利用率 0.8，使用 W16A16。默认可用算力为 249.6 TFLOP/s。系统输出保存导入源码 SHA256 和来源 commit。SM/L1/L2 数量是参数来源，不表示执行了 SM 或缓存模拟。

| 参数 | 单 Stack | 单 GPU（5 Stack） |
| --- | ---: | ---: |
| Capacity | 4 Die × 2 GiB = 8 GiB | 40 GiB |
| Compute | 4 Die × 1 GiB = 4 GiB | 20 GiB |
| 总容量 | 12 GiB | 60 GiB = 64,424,509,440 B |
| 普通 DRAM 接口理论上界 | 665.80 GB/s | 3328.9987 GB/s 聚合接口上界 |
| TSV 预算 | 896 GB/s | 4480 GB/s，各栈独立 |
| UCIe 预算 | 512 GB/s | 2560 GB/s |
| GPU crossbar | 全卡共享 | 2560 GB/s |

`min(5 * min(DRAM, TSV, UCIe), crossbar)` 给出 host-memory 路径理论上界 2560 GB/s。实际端到端吞吐由控制器、packet 大小、队列、依赖和竞争决定，不用该上界替换模拟耗时。AttAcc 的 80 GiB、3352 GB/s、默认 8 卡被明确覆盖；600 GB/s NVLink 不用于栈间路径；不叠加原始 0.85 memory utilization。

容量使用 bytes/GiB（2^30 B），带宽使用十进制 GB/s（10^9 B/s）。GPU arena 分配于每个 Capacity bank 的上半区（全卡 20 GiB），冷 KV 占下半区（20 GiB），热 KV 使用 Compute（20 GiB）。这是显式工程划分，可用总物理容量仍为 60 GiB。GPU tensor 按 stack/channel/PC/die/BG/bank 交错；每个 packet 预留 256 B，尾包只发实际所需 32 B bursts，并携带有效 byte mask。所有实际 migration 请求检查不跨入 GPU arena；容量溢出直接拒绝。

## 时间与流量

`gpu_kernel()` 使用 AttAcc `Layer.get_flops()` 的 `2*m*n*k`，时长为 `ceil(FLOPs / (peak * utilization) / tCK)`，再加配置的 launch cycles。当前 launch=0，未校准小矩阵效率；极小 kernel 可仅占 1 cycle。所有 GPU kernel 竞争全局 `gpu_compute/card0`，不会按 Stack 重复得到整卡算力。计算模型不包含分析式 DRAM 时间。

保守 kernel 依赖是 load → compute → store，不模拟 kernel 内存/计算重叠。GPU load 真实路径为 DRAM/TSV → UCIe → crossbar；store 反向执行。Query 和 PIM output 也有明确 host port 传输，attention 内部 broadcast/gather 则另记 coordinator/TSV 流量。PIM、promotion、demotion、GPU RD/WR 使用同一个 Fabric；只把 GPU compute 作为独立可并行资源。

系统全部使用 769 ps DRAM 时间轴，GPU 时长向上取整误差小于一个 cycle。`foreground_latency = decode_done.complete - issue`；后台终点、所有任务终点、模拟器停止和维护/回调排空差值分别报告，不把重叠阶段耗时相加，不将后台排空收费到前台。`by_source` 的 read/write bytes 是物理 32 B bursts，mask-valid bytes 是接口有效字节；迁移的 FP16 等价 payload 单独输出，不能用 demotion 的全 burst read mask 推断数值 KV 大小。PIM Bank 内部操作数字节未知，未伪造成实际 0 流量。

## 最小流水及验收

`traces/a100a_decode_micro.yaml` 是五头、每头 dim=16、单 query 的合成流量：GPU query projection（80×80 权重）→五栈 PIM attention（每头 8 tokens）→GPU output projection，同时每栈 block 0 promotion、block 1 的 12-token 后台 demotion。权重、输入、冷 KV 和待 demote 的热 KV 在发出时已驻留；省略初始化和 KV append。这个 trace 验证接口与数据依赖，不能称为完整 Decode TPOT、模型吞吐或数值 GPU 执行。

从工作区运行（run ID 必须全新）：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 experiments/tokenstack_arch_20261104/scripts/run_gpu_system.py my-gpu-run
PYTHONDONTWRITEBYTECODE=1 python3 experiments/tokenstack_arch_20261104/scripts/run_acceptance.py my-unified-run
PYTHONDONTWRITEBYTECODE=1 python3 experiments/tokenstack_arch_20261104/scripts/release_hardware.py my-release --clean-build
```

统一验收包括 H1–H6 原有 15 场景、全部单元/集成测试、AttAcc 三基线、8 个 TSV 吞吐场景和 6 个 GPU 系统场景。GPU 场景分别缩减 compute utilization（0.0008，仅敏感性压力）、TSV/UCIe/crossbar（1 GB/s），要求实际完成时间变长且字节不变；延后后台发出验证前台计时独立。测试还覆盖单卡算力互斥、60 GiB 推导、分配边界、非 burst 对齐 tensor、未配置 GPU 拒绝和完成日志篡改拒绝。

正式结果位置为 `results/tsv-gpu-acceptance-20261118-v1-gpu/system_acceptance.json`。每个场景有实际配置、完整 graph、输入 trace、分配与计算 manifest、原始 commands/resources/events/tasks/requests/buffers 和独立审计。离线包包含原始 Ramulator base、完整补丁、构建依赖及 AttAcc 最小 Python 源码/许可证，无网络依赖。

局限：896 GB/s TSV 为共享流体模型；GPU 计算利用率及启动时间未做实机校准；不模拟 cache、kernel overlap、完整 Transformer/MoE、Router/Experts 或多卡/NVLink。论文 Q1–Q5 仍未解决。下一步应先校准 GPU kernel 开销并建立 LLMSimulator 原生 MoE baseline，再经该文件接口接入系统工作负载。
