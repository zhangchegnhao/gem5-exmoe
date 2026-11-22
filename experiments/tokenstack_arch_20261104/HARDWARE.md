# TokenStack 硬件仿真工程配置

2026-11-18。入口为 AttAcc 修改版 Ramulator2 的 `TokenStackFabric` + `TokenStackGraph`；这是 DRAM 命令时序模型和 Base Die 行为/资源时序模型，数值搬运/布局另有功能参考。新增 A100a 算力服务时间模型和五栈系统 Trace 接口，详见 [GPU 系统配置](GPU_SYSTEM.md)。未实现 RTL、GPU SM 指令执行、完整 MoE 系统性能或预测策略。

**工程候选与论文忠实度分开验收。** `config/tokenstack_hardware.yaml` 使用图 3 二进制容量候选：每栈 4 Capacity（各 2 GiB）+4 Compute（各 1 GiB），16 Channel/2 PC。Channel、row、refresh、结果尾延迟和细化资源规则含显式工程假设。论文 Q1-Q5 仍 open；不能把这个 profile 宣称为论文硬件精确复现。固定证据与问题见 `spec/`。

## 实现与精度

| 层级 | 实现 | 验证范围 |
| --- | --- | --- |
| 稀疏地址/物理层 | `hardware_topology.py`，独立 die_id、stack_id、Channel/PC/BG/Bank/row/column，静态 K token-major / V feature-interleaved、冷页 die-fastest 交错 | 首尾/随机地址往返、容量、无跨层/跨栈别名、Bank-local 归约；固定 allocation pitch 避免不同 shape 的 block 别名 |
| DRAM 时序 | 每物理 die 独立的 HBM3-PIM/PIMDRAM 树，沿用 AttAcc timing/refresh/controller | RD/WR/PIM 真正进入控制器；512/1024 Mib/channel 必须显式给 nRFC/nREFISB，缺失/非法参数拒绝 |
| 共享资源 | 可选 controller 仲裁钩子，真实 issue 前原子检查 Channel、PC data、TSV、PIM 和 Base Die 工作单元日历 | Channel/PC 独占；TSV 重叠传输按总速率限额；方向切换、前台/后台源队列与 aging、同龄准入轮转 |
| 数据迁移 | `kv_pipeline.py` 生成实时依赖 DAG，Burst RD→流水转换→整页 layout→masked WR→commit | 大块小 Buffer、每栈/head-group ping-pong SRAM、背压、页冲突锁、源/目标字节、元数据/padding、完整写后驻留提交 |
| PIM/coordinator | `attention_hardware.py`，MACPB 命令编译、SFM marker、显式 query/weights broadcast、score/output gather、配置的 result-ready 事件 | 小形状 CPU 参考、逻辑有效 lane 覆盖、命令/流量守恒、依赖和尾延迟；该布局无额外跨 Bank partial reduction |
| 主机路径 | `host_paths.py`，GPU 普通访问 UCIe、跨栈 source UCIe→GPU crossbar→target UCIe，逐页双端 Buffer | 2/5 Stack，独立每栈预算和聚合 host 限速；未配置跨卡/NVLink 明确拒绝 |
| GPU 系统 | `gpu_system.py` 复用 AttAcc A100a 配置和 `Layer.get_flops()`，单卡算力资源，显式 tensor RD/WR→UCIe→crossbar | 60 GiB、逻辑/物理字节、算力/链路敏感性、前台完成与后台排空分离；无缓存与 CUDA kernel 校准 |

`nCL+nBL` 是正常 RD 数据返回，WR 使用 `nCWL+nBL+nWR` 保守写恢复；请求接受、实际命令 issue、data-ready、PIM result-ready 和任务完成分别记录。Legacy controller 仍在 PIM issue 时回调；只有 Fabric 包装层将其延迟到配置的结果事件。默认 MAC 8、move 4、softmax 32、mode 1 cycles 都是**未校准的工程尾延迟**，不由 nCCD 推导，提供扫参而不是伪称测量。

PIM 单元共享范围采用保守的每 die/channel 结果锁，等待期间同范围命令不能提前改变状态；并非论文已确认 units/BG 的精确实现。MACPB 继承 AttAcc 的跨 PC 广播语义，编译器合并相同命令并记录 valid PC/lane，其余结果丢弃；未实现数值寄存器或可验证的硅级 lane gating。SFM 是 Base Die softmax 行为的 timing marker，CPU 参考执行 softmax，Ramulator 本身不执行浮点算术。

新配置要求 `DRAM.tokenstack_physical_dies=true`，广播预检查扫描全部目标 Bank，先处理任何冲突行后才允许 ACT。此检查默认关闭以保留旧 AttAcc 配置和基线行为；旧模型的基线复现不构成混合行状态下物理正确性的证明。

同一 die/channel 的普通 RD 数据与 WR 写恢复完成前，新 PIM 前置命令不能改变 Bank 状态；反向由 PIM result 锁保护。该保守的局部 drain 补足继承时序表未覆盖的跨模式完成关系，不会阻止其他 die/channel 的并发。TSV 限速扩展 WR 数据段时，写恢复从扩展后的数据结束计时。

## 资源与字节合同

本 profile 选择 `tsv_coverage: dram_data_burst`：DRAM RD/WR 的数据 interval 就是对应 TSV 传输，**不再在 nBL 后追加两段虚构 hop**。每个真实源 RD 和目标 WR 各扣一次所在栈 TSV 字节；nBL 与聚合带宽给出的服务下界取较大值。TSV 896 GB/s 和 UCIe 512 GB/s 是两个独立的十进制每栈预算，没有按 Layer 或 Channel 重复发放。

Channel 命令槽按 stack/channel 共享、data 按 stack/channel/PC 共享，TSV 双方向共用聚合日历；方向 guard 2 cycles 是工程假设。源队列每栈分 GPU/PIM/promotion/demotion，深度不含已进入控制器的请求；控制器每 die/channel 还有限制在途数。准入和工作选择采用 nominal priority + aging，die 遍历轮转，所有 DRAM 时钟与 refresh 持续推进。验证的是固定有限压力窗口内的活性；不是无限对抗到达的形式化无饥饿证明。

2026-11-18 修正：TSV 从“每个 32 B burst 独占全栈”改为 `shared_rate` 流体带宽日历。在任意周期边界区间内，所有重叠传输的 `sum(bytes / duration)` 不得超过 `tsv_GBps * clock_ps / 1000` B/cycle；不同 Channel 的同向 burst 因此可以重叠。相反读写方向仍需 2-cycle guard，方向未指定的 coordinator 消耗带宽且不会清除已有方向 guard。PC 数据区间仍独占。这个规则是工程抽象，未校准真实 TSV lane 分配或交换网络。

每源每周期最多 dispatch 16 条独立 Channel 请求；同龄任务按最近成功准入时间轮转，避免较小编号 Channel 持续填满前端队列。默认队列深度 8，饱和基准显式使用 256，不将其当作论文控制器配置。TSV `busy_cycles` 按区间并集计算，`bandwidth_utilization` 独立统计；不能累加重叠区间作为占用率。

继承时钟与 nBL 给出普通读写接口上界 `16 CH * 2 PC * 32 B / (2 * 769 ps) = 665.80 GB/s/stack`。896 GB/s TSV 预算不等于普通 DRAM 可达到 896 GB/s。`run_throughput.py` 以共同 issue=0 到模拟器结束（含 refresh/drain）为带宽分母；验收包含 1/4/16 CH、448/112/16 GB/s 瓶颈、四 Die 共享和两栈独立预算。旧 H6 没有吞吐下界检查，其通过状态不能作为 TSV 吞吐正确性的证据。

Base Die buffer 按 `T_page*d*2*2 B` 每页/head group，默认 4-token×16-feature =256 B，两槽合计512 B；每栈独立。两个槽允许下页读取与前页转换/layout/写回重叠。`dma_engines` 控制转换和 layout 的执行 lane 数，单个引擎内部允许页流水，描述符/page 在途由 SRAM 槽约束；不是任意并行整块 DMA。转换为 burst 级 streaming，匹配带宽时仅保留流水边界，不额外收取整页串行量化时间；transpose 仍是单独可限速阶段。

K8V4 的 3/8 仅指相同元素数 K/V payload，不含每组 FP16 scale 或 burst padding。工程 codec 是对称分组量化、round-to-nearest、饱和、FP16 scale，INT4 signed nibble 打包；不声称复现论文精度损失。记录 K/V payload、metadata、cold burst padding、hot scatter burst amplification。hot page 跨 burst/共享 burst 时使用显式 byte mask，按完整 DRAM burst 收费；普通 masked-write 是本 profile 的接口假设，未加入未建模的 RMW。

数据功能参考使用稀疏 FP16 字节执行 quant/dequant/scatter/transpose/gather，并与 CPU attention 比较；实时 Ramulator 只消费请求地址和 byte mask，不保存整份数值 KV。二者是分开的功能和时序验证，不能称为逐 cycle 数值联合执行。

## Trace 与日志

Graph YAML schema v1 的任务有唯一 `id`、`source`、`issue_cycle`、`depends_on`。内存节点含 `requests: [{type,address,byte_mask?}]`，地址为全局**字节地址**；type 0/1 是 RD/WR，4..13 是继承 AttAcc PIM 类型。工作节点含 `resource/stack/bytes/cycles`。Buffer acquire/release 是独立节点，page_lock 防止同一逻辑页双向同时迁移。未指定依赖表示允许并行，调用方必须表达数据依赖；attention 的 dependent promotion 模板已自动连接。

内存分配的 block ID 在所有 head/group 内全局唯一，head_group 指 Buffer 池分区。每 Bank block stride 和 cold pages/block 在 YAML 中显式给定；逻辑块超过保留范围会拒绝。跨栈 raw copy 保持字节格式，不隐式承担 K8V4 转换；需要转换的调用方使用显式转换 DAG。

所有时间为 769 ps DRAM cycle 边界，日历为 `[start,end)`，不沿用旧 guard CSV 的边界语义。

- `commands.csv`：实际发出的 command、source、request_id、stack/die 和完整 DRAM 坐标；维护请求 ID=-1。
- `resources.csv`：每资源实际保留区间、tag、source、bytes、direction；内存 tag 是 request_id，工作 tag 是 task_index，二者依 resource/direction 区分。
- `events.csv`：accept、issue、operand-ready、data-ready、result-ready、complete；不适用字段为 -1，不表示零时延。operand-ready 对 PIM 是显式依赖后接受的命令边界假设，不是数值寄存器测量。
- `requests.csv`、`tasks.csv`：唯一 ID、依赖完成时刻、物理请求与 masked bytes；`buffers.csv` 保存分配、释放和占用。
- `verification.json`：任务/请求/字节/时钟守恒、资源占用和有效带宽、访问延迟、Buffer 峰值、refresh 命令。完成时间区分最后任务与模拟器结束，后者可能包含维护 drain。

未知 PIM 内部操作数字节不伪造为真实 0；TSV 流量只包含显式数据传输，MAC 的 Bank-local 读不额外收费。输出的利用率来自资源日历，不是 command grant 比例。所有数值是本模型输出，不能当作真实 HBM 或论文端到端性能。

## 一键验收与发布

从 `/home/ubuntu22/gem5` 执行，新 run ID 不得复用：

```
cmake --build attacc_simulator/ramulator2/build -j2
PYTHONDONTWRITEBYTECODE=1 python3 experiments/tokenstack_arch_20261104/scripts/run_acceptance.py my-hardware-run
PYTHONDONTWRITEBYTECODE=1 python3 experiments/tokenstack_arch_20261104/scripts/run_throughput.py my-throughput-run
PYTHONDONTWRITEBYTECODE=1 python3 experiments/tokenstack_arch_20261104/scripts/run_gpu_system.py my-a100a-run
PYTHONDONTWRITEBYTECODE=1 python3 experiments/tokenstack_arch_20261104/scripts/run_acceptance.py my-five-stack-run --profile tokenstack_paper_profile --allow-engineering-assumptions
PYTHONDONTWRITEBYTECODE=1 python3 experiments/tokenstack_arch_20261104/scripts/release_hardware.py my-hardware-release --clean-build
```

`tokenstack_smoke` 是默认工程验收矩阵；`tokenstack_paper_profile` 是五栈候选的显式入口，必须标明采用工程假设，仍执行完整 1/2/5-stack 矩阵。严格论文冻结门禁另用 `audit_hardware_contract.py <new-id> --require-paper-freeze`；当前预期返回2。旧 conservative 模式继续使用原 `run_layered.py` 和 `tokenstack_layered.yaml`，不与新拓扑结果直接计算“加速比”。

发布脚本保存完整 upstream-base→当前源码补丁（含 AttAcc 和全部 untracked 扩展）、base source archive、固定依赖源码/commit/hash、Python 工具/Trace/规格，以及实际导入的 AttAcc config/model/type 和许可证。`--clean-build` 在临时目录应用补丁、逐文件 hash 核对，以 GCC12/C++20 Release 全新离线编译，运行 smoke、完整工程矩阵、全套测试、AttAcc 三基线、吞吐验收与 GPU 系统验收；不清理/重置用户工作区。依赖包保留上游许可证。新机器需 Python3.10+、PyYAML、CMake、GNU C++12 和相容系统库；本地依赖缓存随发布包归档，不要求网络下载。

没有校准证据的物理参数、Q1/Q2 原文矛盾和 Q3-Q5 微架构缺失继续阻止论文忠实度验收。H6 工程发布的通过状态以相应目录 `manifest.json` / `acceptance.json` 为准，不能代替 roadmap 的严格论文完成判据。
