# 2026-11-18 当前模型源码审查

结论：TokenStack 已有可执行且可复现的拓扑、DRAM 命令后端、共享仲裁、迁移 DAG、PIM 结果事件与多 Stack 路径。原 H6 的功能/守恒/回归结果有效，但不能作为聚合吞吐合理性的证明。本次发现普通 RD/WR 的整栈 TSV 独占粒度会严重压低带宽，需要修正 H2 资源模型并补充 H6 带宽验收。

## 当前实现

| 模块 | 源码位置（相对工作区） | 能力及精度 |
| --- | --- | --- |
| 拓扑、映射 | `experiments/tokenstack_arch_20261104/hardware_topology.py:67` | 1–5 stacks、显式 die 几何/容量/地址检查、K/V 静态映射；工程候选 |
| 每 die DRAM 后端 | 同文件 `ramulator_layers():149`；`attacc_simulator/ramulator2/src/memory_system/impl/tokenstack_fabric.cpp:141` | 每 die 实例化 PIMDRAM/HBM3-PIM/controller，独立 Bank/row/refresh 时序 |
| 共享仲裁 | `tokenstack_fabric.cpp:49`、`:65`、`:109` | 资源日历、来源队列、aging、真正命令发出前挂钩；TSV 吞吐缺陷见下文 |
| 迁移 | `experiments/tokenstack_arch_20261104/kv_pipeline.py:74` | 逐页读/转换/布局/写/commit，元数据及 padding 计费 |
| Buffer 与依赖 | `attacc_simulator/ramulator2/src/frontend/impl/memory_trace/tokenstack_graph.cpp:110` | callback 驱动 DAG、有界 SRAM 占用、页锁、释放；不存储真实运行时 tensor 数据 |
| Attention | `experiments/tokenstack_arch_20261104/attention_hardware.py:8` | broadcast、MACPB、gather、SFM 标记、显式 result-ready；Python 数值参考独立 |
| 主机路径 | `experiments/tokenstack_arch_20261104/host_paths.py:6`、`:20` | UCIe 与跨 Stack GPU crossbar 行为时延；未配置 NVLink 请求拒绝 |

当前 runtime YAML 是每栈 4×2 GiB Capacity + 4×1 GiB Compute；默认单栈，测试扩展到五栈。每 die 16 Channels、2 PCs；Capacity 16 banks/PC、Compute 8 banks/PC。时钟 769 ps、burst 32 B。page_tokens=4、head_dim=16、两个 page slots，对应每 Stack/head group 512 B Buffer。以上参数含明确工程假设，不能整体当作论文确定参数。

## 原验收与本次核对

当前 H6 快照的 48 项哈希（包括源码、配置和可执行文件/动态库）与 `../h6-engineering-v6/metadata.json` 一致。原记录显示 15 场景、55 测试通过；`../h6-release-v3/manifest.json` 记录离线重建150文件、干净编译与测试通过，AttAcc 273/822/2094 cycles 不变。本次读取并核对这些历史结果，未再次编译或重复运行全套测试。

本次新增且实际运行的诊断命令（工作区 `/home/ubuntu22/gem5`）：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 experiments/tokenstack_arch_20261104/results/model-review-20261118-v1/probe.py
```

探针在一个 Capacity die 的 16 Channels × 2 PCs 各发出32个读请求，共1024请求/32768 B；不经过主机 UCIe，用于隔离内存资源模型。原始配置、请求、命令、资源、回调与验证输出在 `tsv-896/`、`tsv-448/`、`tsv-16/`，汇总见 `review.json`。这些均是仿真器输出，不是硬件实测。

| 配置 TSV GB/s | 完成 cycles | 有效 GB/s | 每 burst TSV 区间 cycles |
| --- | ---: | ---: | ---: |
| 896 | 4206 | 10.1310 | 2 |
| 448 | 4206 | 10.1310 | 2 |
| 16 | 4259 | 10.0050 | 3 |

## 本次新增发现

`tokenstack_fabric.cpp:88` 为每个32 B RD/WR计算 `max(nBL, transfer(32,m_tsv))`；`:91` 将整个区间保留在唯一的 `tsv/s<stack>` 资源上，`:49` 拒绝任何区间重叠。默认 nBL=2，所有 die/channel/PC 的普通数据 burst 因此在栈级互斥，理想上限为：

```
32 B / (2 × 769 ps) = 20.8062418726 GB/s
```

该上限远低于声明的896 GB/s。探针的10.13 GB/s还包含控制器调度/排队及首尾开销，不能当作饱和带宽上限；源码推导说明即使增加并发也无法突破20.81 GB/s。该结论仅针对普通 RD/WR，coordinator 工作节点使用另一种大块字节服务规则。

既有 `test_shared_command_tsv_and_independent_channels` 检查了同周期跨 Channel 命令和总字节数，但没有检查跨 Channel 数据传输的合理并发与吞吐。资源互斥和字节守恒通过仍可能过度串行化。因此应保留旧结果，同时将性能验收状态视为需要修正；不能仅以等待作者公开参数解释此实现限制。

## 其他边界及建议顺序

PIM result-ready 目前按配置尾延迟生成，且保守锁住 die/channel；没有真实 BG 级执行单元、寄存器数据或数值 MAC/softmax 仿真。量化与布局数据校验是独立 Python 参考，timing DAG 中的 byte mask 仅用于描述和审计。迁移驻留提交表现为 DAG commit 节点，完整动态 KV 页表、缓存淘汰/预测策略和 MoE 端到端闭环尚未实现。五栈路径支持不等于完整 GPU 或八卡系统模型。

下一项优先将 Channel/PC 的 nBL 占用与栈级按字节计费的共享带宽约束分开表达，允许符合总预算的独立 Channel burst 重叠，并用单/多 Channel 饱和微基准验证；还需统一继承 HBM3 时钟/峰值带宽与896 GB/s链路参数的来源及适用条件。然后重跑迁移/attention/refresh/旧 AttAcc 回归，重新交付工程性能验收。论文 Q1–Q5 证据问题另行保留。
