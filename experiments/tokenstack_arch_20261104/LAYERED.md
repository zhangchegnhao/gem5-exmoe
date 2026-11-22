# Capacity / Compute 独立时序域

本步在原 AttAcc 闭环之上新增 `TokenStackMemory` 组合 memory system：分别创建两个完整的 `PIMDRAM` 实例，并分别执行 controller、DRAM、mapper、refresh、plugin 的 setup。两层的 Bank/row 状态、请求队列和时序表属于不同对象，不再用同一个 DRAM 对象里的两个地址区模拟 Layer 隔离。

Capacity 复用 AttAcc `HBM3-PIM` 模型的普通 RD/WR 路径，组合层拒绝任何发往 Capacity 的 PIM（含 barrier）。Compute 允许 PIM。两层均沿用 `HBM3_8Gb_2R` 实验组织，不表示论文中每个物理 Die 的容量、rank 结构、层数已经校准。精度是 **两个独立的 Ramulator2 DRAM 命令时序域 + 保守的 Stack/Base Die 行为仲裁**，不包含数据内容或 RTL。

## 配置、路由与共享资源

中心配置 `config/tokenstack_layered.yaml` 显式列出 `MemorySystem.Capacity` 和 `MemorySystem.Compute`，各自允许独立 DRAM 时序参数。两层目前必须同 tCK、单 Channel、1:1 前端/内存时钟、`HBM3-PIM` 映射器；不支持隐式跨时钟转换。

最小实验仅映射两段 1 MiB 窗口：Capacity 全局基址 0，Compute 全局基址 1 GiB。它们是**实验地址窗口，不是物理 Layer 容量**。组合层将全局地址减去 Layer 基址后送入各自 mapper，所以两层都可以使用层内地址 0，也各有自己的同坐标 Bank/row。越界、地址空洞、未对齐 burst、前后端 Layer map 不一致会显式失败；不把高地址位截断后路由到另一个层。

继续保留一条统一的 attention/promotion/demotion 事务队列、一个活动事务、一个 DMA 和整页 Buffer 保留。新增诊断 `access` job，指定 `layer: capacity|compute`，每个 job 只有一条 `LD` 或 `ST`；使用 `depends_on` 表达访问顺序。工作负载 schema 仍为 v1，旧 attention/迁移 trace 无需变更。

Stack 仲裁器每周期最多允许一个 Layer controller 调度命令，包含 refresh 命令；同一 controller 原有的 ROW/COL 双发能力保留。两层 DRAM 时钟、read/write 回调和 refresh 入队始终推进，不能通过冻结另一个 DRAM 的时钟伪造隔离。

跨层切换先排空当前层的 pending（包括读返回、写恢复），再等待 `domain_switch_guard_cycles`。当前 64 cycles 是合成保守参数，单位为实际 DRAM cycle（当前 769 ps），不是论文测量值。初始化要求该值至少覆盖两层 RD/WR 返回、相关 burst/turnaround 和 MAC 命令间隔下界。此设计避免独立时序表绕过 Stack 共享路径约束，但可能过度串行化；授予的是 controller 调度机会，不是实际总线利用率。刷新可被事务和门控延后，本实验不证明 refresh deadline/retention 合规。

内部 source/target TSV/Base Die hop 仍为 DRAM 数据总线以外的额外串行链路抽象，量化/解量化假设吞吐匹配。896 GB/s 是整 Stack 的论文实验假设，两层不会各自获得此带宽。不同层实际 TSV 路径、转换元数据、重叠以及物理 bus 时序均 `TODO: verify`。

## 实现文件

- `ramulator2/src/memory_system/impl/tokenstack_memory.cpp`：独立 Layer 后端初始化、路由、Stack 命令准入/门控、跨层切换和逐层统计。
- `ramulator2/src/memory_system/tokenstack_memory.h`：可选两层接口，不修改原通用 IMemorySystem 接口。
- `ramulator2/src/memory_system/CMakeLists.txt`：注册新实现。
- `ramulator2/src/frontend/impl/memory_trace/tokenstack_trace.cpp`：可选两层接入、层内回调延迟、显式 Layer 日志与单操作 access job；默认单域模式保留。
- `scripts/run_layered.py`、`tests/test_layered.py`：真实 controller 实验、逐 burst/命令审计、隔离和无效输入集成测试。
- `traces/layer_isolation_v1.yaml`：依次测试同坐标冷访问、row hit/conflict、Compute PIM 广播后 Capacity row hit。

新增请求日志 schema **v2** 在 v1 后增加 `layer_id,local_address,tck_ps`，Layer 0=Capacity、1=Compute。`address` 始终为原全局地址，`addr_vec`/原始命令日志按层内地址解码。回调期间按目标层真实时序计算 RD/WR 命令周期，并与该层原始 command log 一一核对。旧单域仍输出 schema v1。

新增 `grants.csv` schema v1：`cycle,owner_layer,external_blocked,capacity_pending,compute_pending`（另有 `schema_version`）。-1 表示该周期不允许任何 Layer controller 发命令。验证器检查每条实际命令所属层的 grant、跨层切换间隔、两个 Layer 的完整时钟、Stack 级门控周期只计一次以及逐层接受计数。组合层 stdout 中 `LayerStats` 保留两个原始 `PIMDRAM` 统计。

PIM 回调仍是末命令发出或 barrier 删除，不是计算结果完成。仿真停止周期等于最后 workload 回调或最后维护命令发出的最大周期；若存在待清空的 refresh，允许停止周期晚于 workload 事件，分别记录，不能把维护尾部伪装为额外请求完成。

## 复现与结果

在 `/home/ubuntu22/gem5` 执行，Python 依赖继续为现有 PyYAML；构建沿用 g++-12、C++20、Release：

```bash
cmake --build attacc_simulator/ramulator2/build -j2
PYTHONDONTWRITEBYTECODE=1 python3 \
  experiments/tokenstack_arch_20261104/scripts/run_layered.py NEW_RUN_ID
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=experiments/tokenstack_arch_20261104 \
  python3 -m unittest -v \
  experiments/tokenstack_arch_20261104/tests/test_layered.py \
  experiments/tokenstack_arch_20261104/tests/test_closed_loop.py \
  experiments/tokenstack_arch_20261104/tests/test_tokenstack_model.py
```

每个 run ID 必须全新。正式记录在 `results/layered-domains-v3/`。v1 是在增量构建失败后旧二进制产生的早期诊断，不能作为新模型结果；v2 是已通过的中间结果。v3 保存配置/trace、原始命令和事件、逐周期 grants、逐层统计、工具版本、二进制/动态库 hash、源文件快照、编译设置、测试和补丁验证日志。

| 场景 | 请求数 | 仿真终点 cycles | TSV/Base Die 门控 cycles | 跨域切换等待 cycles |
| --- | ---: | ---: | ---: | ---: |
| 无迁移 | 12 | 42 | 0 | 0 |
| 两次 8 KiB 迁移，896 GB/s | 716 | 4148 | 34 | 94 |
| 两次迁移，28 GB/s | 716 | 5068 | 1048 | 0 |
| 仅 Capacity nCL 从 19 改为 26 | 716 | 4155 | 34 | 94 |
| 3.5 GB/s + 两层 refresh | 716 | 13351 | 8372 | 64 |
| 同坐标访问/PIM 隔离 | 12 | 509 | 0 | 256 |

正常迁移路由：Capacity 96 RD / 96 WR / 0 PIM；Compute 256 RD / 256 WR / 12 PIM（含 barrier）。逻辑迁移共 16384 B，物理读/写各 11264 B。Buffer 峰值为 8192 B。逐层接受计数、逐 burst 地址/字节、末命令、回调和最终时间均守恒；六组 grant 日志都未出现两层同周期发命令。

隔离 trace 中 Capacity 只产生 row 0 和 row 1 各一次普通 ACT、一次 PRE，随后两次 row hit 不再 ACT；Compute 自己的 row 0 首访需要独立 ACT，Capacity 冲突后 Compute 再访仍命中。Compute 的 PREA/ACTAB/MACAB 仅出现在 Compute 日志，之后 Capacity row 1 仍命中。单独修改 Capacity nCL 后，其 RD 返回间隔变为 28 cycles，Compute 仍为 21 cycles，验证两层时序表独立；排队/依赖会传播总时间变化，不声称系统延迟互不影响。

4 项新增测试（8 个无效设置/直接路由子用例，另含日志篡改检查）和原有 11 项测试通过。关闭新 memory system 时原 AttAcc 全部内存统计不变，bank/BG/buffer 为 273/822/2094 cycles，记录在 `results/layered-domains-baseline-regression-v1/`。这些是配置下仿真与接口正确性结果，不是硬件收益或论文性能复现。

增量补丁 `patches/tokenstack-layered-domains-v1.patch` 在既有 retirement 和 closed-loop 两份补丁之后应用；完整链在临时目录应用后与当前源码逐字节核对。gem5 源码/构建和所有旧结果保留。

## 下一步

先将迁移改为有 Buffer 背压的分块 DMA，并解决等待队列的准入优先级反转，再评估前台与后台按块交错。当前 Layer 队列/row 状态已分开，但命令和事务仍保守串行；多 Channel/Stack、完整论文 Layer 组织、物理链路校准、PIM 结果尾延迟、端到端 MoE 尚未实现。
