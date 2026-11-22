# TokenStack Phase 2/3：Ramulator2 闭环迁移与离线接口

## 当前交付

2026-11-18 更新：TSV 已采用共享速率仲裁，并修复前端同龄任务准入偏置，加入真实吞吐门槛验收；新增 [A100a + 五栈 TokenStack 系统配置](GPU_SYSTEM.md)。统一验收结果见 `results/tsv-gpu-acceptance-20261118-v1/`，其 `-throughput` / `-gpu` 相邻目录分别保存饱和吞吐与系统流水；离线发布见 `results/tsv-gpu-release-20261118-v1/`，须读取 manifest 确认构建状态。下段 H6 是历史发布，旧资源验收不能证明修正前 TSV 吞吐正确。

2026-11-05 新硬件入口为 [TokenStackFabric 工程配置与复现](HARDWARE.md)：显式 4+4 die、共享资源日历、逐页 DMA/Buffer、K8V4 功能参考、PIM result-ready、Bank-local attention 和 2/5-stack 主机路径均已有实现与测试。最终工程验收入口 `scripts/run_acceptance.py`，离线源码/补丁发布入口 `scripts/release_hardware.py --clean-build`；本轮结果见 `results/h6-engineering-v6/` 和 `results/h6-release-v3/`。是否通过分别读取目录内 `acceptance.json` / `manifest.json`。**论文规格仍未冻结，工程配置通过不代表论文硬件忠实复现完成。** 下文的双域与单域路径保留为历史对照。

最新进展：已经新增两个独立 DRAM/controller 时序域与 Stack 级共享命令仲裁，详见 [Capacity/Compute 独立时序域](LAYERED.md) 和 `results/layered-domains-v3/`。下文保留单域闭环和更早离线模型的实现/结果，便于分别复现。

后续执行顺序以 [2026-11-05 硬件完成路线图](HARDWARE_ROADMAP_20261105.md) 为准：先完成 H0 论文规格/参数台账，再推进完整拓扑与逐页 DMA。路线图包含完成边界、论文依据、H0–H6 依赖和验收条件，替代旧章节中直接从原型进入 DMA 优化的下一步建议。

H0 历史记录：[硬件合同](spec/paper_hardware_contract.md)、[机读参数台账](spec/hardware_parameters.yaml) 和 [未决问题](spec/open_questions.md)，结果 `results/h0-contract-v1/`。随后 H1 已补上显式 refresh 安全路径，Fabric 已补配置的 PIM result-ready；这些实现没有解决论文 Bank/容量、单位和未公开微架构的证据问题。原始双域配置与历史结果保留。

新增 `TokenStackTrace` C++ 前端，将迁移真实读写、前台 PIM 请求和共享资源门控放入同一次 Ramulator2 运行。精度为 **原 AttAcc DRAM/PIM 命令时序 + 保守、不可抢占的 Base Die 事务行为模型**。这是单进程闭环驱动，尚不是完整 TokenStack 多 Die 硬件模型。

此前的 `tokenstack_model.py`、`ramulator_bridge.py` 和离线实验继续保留，二者与新闭环路径分别运行；离线结果不含迁移对真实 controller 的反馈。

外部来源：TokenStack arXiv:2605.05639v2 §4/5/7（https://arxiv.org/html/2605.05639）。896 GB/s 是论文实验配置假设；层数、容量、Buffer 和 page 参数属于最小实验设置，见 YAML 的来源/`TODO: verify` 注释。

## 闭环执行路径

新增源码 `ramulator2/src/frontend/impl/memory_trace/tokenstack_trace.cpp`，通过独立可选 `ITokenStackController` 能力接口调用 HBM3-PIM controller 的命令门控；`Controller.tokenstack_closed_loop` 默认关闭。

- attention、promotion、demotion 共用有限等待队列和一个活动事务；已准入事务按 attention > promotion > demotion、issue cycle、trace 顺序选择，活动事务不可抢占。事务整个生命周期独占 Channel/Base Die，迁移额外占用 TSV。不存在两个 Layer 各自获得完整 Stack 带宽的情况。
- 每次迁移保留一个完整逻辑页的 Buffer 容量，超过 Buffer 的迁移明确拒绝；没有静默截断或虚构分块。单 DMA，队列深度不含活动事务；队列满时尚未准入的任务等待，因此可能出现准入造成的优先级反转，见 small-queue 用例。依赖未满足时不占队列。
- 源 RD 以配置的 32 B burst 进入真实 controller，等待 `nCL+nBL` 数据返回；随后经过 source TSV/Base Die hop、target TSV/Base Die hop，再执行目标 WR，并等待 `nCWL+nBL+nWR` 保守写恢复边界。当前读返回为 21 cycles、写恢复为 29 cycles，均来自实际 DRAM 时序表。
- 目标写描述符在 target hop 开始时就进入 controller。门控禁止整个 hop 期间发出任何命令，直到数据可用后才允许 ACT/WR；已有数据返回、写恢复回调、DRAM 时钟和 refresh 入队继续推进。原始命令日志用于检验这一点，不以“队列为空”替代阻塞验证。
- 两个 hop 是 **DRAM `nBL` 数据总线之外另加的、串行的内部链路抽象**；没有再次估算并叠加 RD/WR 服务时延。内部 hop 是否应与 DRAM 总线重合/重叠、量化流水吞吐、精确 TSV 路径均待硬件校准。896 GB/s 是论文实验假设，首版把整 Stack 作为单资源保留，不是每 Channel 各有 896 GB/s。量化/解量化假设吞吐匹配，不另加虚构耗时。

Capacity/Compute 目前是同一真实 HBM3-PIM 实例里的两段不重叠地址区（0/1 MiB 起点，各 1 MiB），共享 Bank/时序状态；它们 **不代表两个独立物理 Die**。初始化检查地址解码后的完整范围、burst 对齐、映射器和单 Channel 边界。跨物理 Layer 的 bank 隔离、PIM 广播范围尚未实现。

## 闭环 Trace、日志与复现

中心配置：`config/tokenstack_closed_loop.yaml`，工作负载：`traces/closed_loop_kv_v1.yaml`，均保留参数单位/来源。workload schema v1 包含 `id/kind/issue_cycle/depends_on`；迁移包含 `offset/logical_bytes`，attention 包含 `op/offset` 请求数组，地址相对对应 Layer region。当前只支持单 Stack、单 Channel、`HBM3-PIM` 映射器、前端/内存 1:1 时钟。未知依赖、依赖环、越界、零带宽、buffer 不足、burst 不匹配会显式失败。

```bash
cd /home/ubuntu22/gem5
cmake --build attacc_simulator/ramulator2/build -j2
PYTHONDONTWRITEBYTECODE=1 python3 \
  experiments/tokenstack_arch_20261104/scripts/run_closed_loop.py closed-loop-next-run
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v \
  experiments/tokenstack_arch_20261104/tests/test_closed_loop.py
PYTHONDONTWRITEBYTECODE=1 python3 \
  experiments/tokenstack_arch_20261104/scripts/verify_ramulator_regression.py baseline-next-run
```

每个 run ID 必须全新。脚本依次运行无迁移、正常迁移、28 GB/s 链路、深度 1 队列、3.5 GB/s 跨 refresh 压力五个场景。输出包括每个场景的完整 YAML/workload/命令、stdout/stderr、原始 command log、三类 schema v1 CSV、汇总、C++/Python 源码与测试快照、CMakeCache/编译命令、binary 和动态库 hash、工具版本。

- `requests.csv`：每次实际接受请求的唯一 ID、所属 job/phase/index、类型/地址/Channel、accept/command/event/callback cycles、物理字节和事件类型。RD/WR 的 `command_cycle` 从回调边界减实际时序延迟导出，并逐条与原始命令日志核对；barrier 对应 scheduler 删除事件，没有真实 DRAM 命令。PIM 字节数为 0（未知、不统计）。
- `jobs.csv`：issue/eligible/admit/start/complete cycles、逻辑字节与源读/目标写物理字节。
- `resources.csv`：事务保留区间与两个额外链路区间/字节。以边界时刻记区间长度；controller 禁止区间内 `start < command_cycle <= end` 的命令发出。

`complete_cycle` 的语义随事件变化：RD 是数据返回，WR 是保守写恢复边界；attention 是所有 PIM 指令末命令发出/barrier 删除，**不是算术结果或端到端 attention 完成**。最终停止周期必须等于最大 job/event cycle；写恢复和后台 demotion 不能提前退出。所有周期采用实际 tCK（当前 769 ps），不沿用旧 Python 模型的 1 GHz。

## 闭环验证结果

正式结果见 `results/closed-loop-v4/summary.json`。`closed-loop-v1` 是早期校验脚本诊断；v2/v3 是通过的中间记录，其中 v3 的 metadata 误取系统默认 C++ 11 版本，实际构建使用其 CMakeCache/compile_commands 中的 g++-12，v4 已改为从 Cache 导出实际编译器（12.3.0）。同一份两次 attention、一次 promotion 和一次 demotion 的小工作负载：

| 场景 | PIM 请求（含 barrier） | 迁移 RD/WR 请求 | 仿真终点 cycles | 额外链路门控 cycles |
| --- | ---: | ---: | ---: | ---: |
| 无迁移 | 12 | 0 / 0 | 42 | 0 |
| 896 GB/s 链路 | 12 | 352 / 352 | 4073 | 34 |
| 28 GB/s 链路 | 12 | 352 / 352 | 5087 | 1048 |
| 深度 1 等待队列 | 12 | 352 / 352 | 4073 | 34 |
| 3.5 GB/s + refresh | 12 | 352 / 352 | 13283 | 8372 |

两次迁移逻辑字节共 16384 B；物理读/写各 11264 B；Buffer 保留峰值 8192 B。正常顺序为 promotion → attention-0 → attention-1 → demotion；队列深度 1 出现 2 个准入阻塞周期，顺序变为 promotion → demotion → attention-0 → attention-1，明确暴露有限队列的优先级反转。refresh 压力场景执行 4 条 refresh 命令；门控期间仍入队、释放后正常执行。这只是命令/调度健壮性验证，不是 refresh deadline 或数据保持时间合规证明。

脚本核对所有 request/job ID、逐 burst 地址/类型/字节、RD/WR 回调延迟、实际发出的末命令（不含 barrier）、依赖、Buffer/队列上界、共享资源无重叠、链路周期以及停止时间。3 个新增 integration test（含 11 个无效输入子用例、丢失/重复/字节/时间日志破坏检查）和原 8 个 Python 测试通过。关闭 TokenStack 模式的全部原内存统计保持不变，bank/BG/buffer 为 273/822/2094 cycles，见 `results/closed-loop-baseline-regression-v1/`。

闭环增量补丁 `patches/tokenstack-closed-loop-v1.patch` 应在 AttAcc 原始扩展和 `ramulator-retirement-v1.patch` 之后应用；没有覆盖这两个基线。不能把本次闭环终点与旧离线 60 cycles 直接比较：工作负载数量和 DRAM 迁移服务覆盖范围不同。

## 离线接口与时间语义（历史路径，继续保留）

在 Ramulator2 `PIMLoadStoreTrace` 启用可选 `completion_path`，输出 schema v1 CSV：

```text
schema_version,source_id,type_id,address,channel,arrive_cycle,retire_cycle,tck_ps,event
```

`source_id` 是输入 trace 的零基行号。`channel` 取自 Ramulator2 的真实地址解码。`arrive_cycle` 是 controller 接受请求的周期；`retire_cycle` 是末条 DRAM/PIM 命令发出（`final_command_issued`）或 scheduler 删除 barrier（`barrier_removed`）的周期，**不是 MAC/softmax 计算完成周期**。时钟周期从实际实例的 `get_tCK()` 导出；当前 HBM3 配置为 769 ps。

回调没有改变 PIM 的退出条件。frontend 在 `finalize()` 显式 flush。controller 还修复了删除请求后继续访问 iterator、第二请求使用错误 buffer 指针的问题。三处 C++ 增量相对 AttAcc 原始扩展保存在 `patches/ramulator-retirement-v1.patch`；既有 AttAcc 基线补丁保持原样。不要再次运行 AttAcc 自带 reset 脚本覆盖这些增量。

## 回放与共享资源

1. 验证输入请求 ID、类型、地址、时钟、退出数和时间顺序。
2. 在单 Channel 时间线上使用相邻退出事件之间的间隔，包含原始 prerequisite/DRAM 等待，各间隔只计算一次；不把每条 `retire-arrive` 延迟相加。
3. 先无迁移回放，要求每个事件周期和最终时间线与 Ramulator2 一致。
4. 在同一个 `ResourceArbiter` 中插入 promotion/demotion。Channel 与 Base Die 保守地被整段事件间隔占用；迁移另外占用 TSV，不能抢占已开始的事件。背景优先级目前只在相同发出周期时参与排序。

该回放把单 Channel 的空隙也计入保留区间，因此可用于接口/守恒验证，不能用于预测真实可重叠调度收益。多 Channel 需要显式跨 Channel 依赖，当前 adapter 会拒绝多 Channel 退出日志。PIM 请求计数包含 barrier；桥接不为每条 PIM 指令虚构 32 B DRAM 数据量，因此桥接中的 PIM 数据字节为 0（未知/不统计）。老的纯行为 smoke 中 32 B 是合成占位统计，不能视为真实 PIM 访存量。

## 迁移字节口径

逻辑字节统一为 FP16 等价大小。K8V4 模式中：

- promotion：Capacity 读取压缩字节，Compute 写入 FP16 字节；
- demotion：Compute 读取 FP16 字节，Capacity 写入压缩字节。

8192 B FP16 等价页对应 3072 B K8V4 数据。暂不含量化 scale/zero-point 元数据。量化/解量化按吞吐匹配 DMA 的流水假设处理，不再将“压缩减少的字节数/带宽”当作额外量化耗时。迁移耗时为源 TSV read 和目标 TSV write 周期相加。

## 复现

工作目录 `/home/ubuntu22/gem5`，依赖 Python 3.10 与 PyYAML（可用已有 `.venvs/attacc/bin/python`）。每次传入新的 run ID，脚本拒绝覆盖已有结果。

```bash
cmake --build attacc_simulator/ramulator2/build -j2
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=experiments/tokenstack_arch_20261104 \
  python3 experiments/tokenstack_arch_20261104/scripts/run_ramulator_bridge.py bridge-next-run
PYTHONDONTWRITEBYTECODE=1 python3 \
  experiments/tokenstack_arch_20261104/scripts/verify_ramulator_regression.py logging-next-run
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=experiments/tokenstack_arch_20261104 \
  python3 -m unittest -v experiments/tokenstack_arch_20261104/tests/test_tokenstack_model.py
```

桥接结果包含输入 trace、实际 YAML、原始 stdout/stderr、退出 CSV、原始命令日志、无迁移和有迁移的事件 CSV、汇总、软件 commit/binary hash、此次代码快照。失败也会保留运行目录与诊断输出。

## 已记录结果

`results/ramulator-retirement-v2/bridge_summary.json`：

- 6 个 PIM 请求退出（含 1 个 barrier），逐一 ID/类型/地址对应；
- 原始 Ramulator2 停止周期与无迁移回放均为 26；
- 插入两次 8192 B 迁移的保守回放终点为 60，新增 TSV 保留 34 cycles；
- 逻辑迁移 read/write 均为 16384 B；物理 read/write 均为 11264 B；
- 请求 ID、物理字节数与时间线长度检查通过。

`results/retirement-logging-regression-v2/regression.json`：启用 callback 日志后，bank/BG/buffer 的全部内存统计与保存的基线一致；退出数分别为 153/345/1273，含 barrier 80/176/224 个，每个输入 ID 恰好退出一次；周期保持 273/822/2094。关闭日志的回归也保存在 AttAcc 实验 `20261104-retirement-regression`。

`results/contention-byte-fix-v2/` 是压缩方向修正后的纯行为竞争结果。原 `20261104-topology-smoke`、`20261104-contention-smoke` 的旧数据原样保留；其中 promotion 源字节、量化附加周期与 PIM Base Die busy 计账已被本次更正，旧数值不应再作为当前模型结果。`20261104-ramulator-bridge` 是早期失败诊断目录，不是有效结果。

## 离线路径边界与后续方向

旧 Python 离线路径的 DMA queue depth 尚未实现准入，统一 arbiter 尚未实现分页占用/背压；上述 C++ 闭环路径已经增加有限准入和整页 Buffer 保留，但二者不是同一个实现。当前仍不支持真实跨 Layer 容量分配、Value transpose、跨 Stack 迁移或 GPU UCIe 请求。闭环迁移有真实 DRAM Bank/row 时序约束，但不同物理 Layer 的 Bank 隔离尚未建模；离线桥接仍仅使用真实 Channel 字段。

下一步是为闭环引入明确的 Capacity/Compute 物理时序域及统一 Channel/TSV 仲裁，验证 Layer 资源隔离，再改为可分块、可重叠的 DMA 调度并处理准入优先级反转。PIM 结果完成尾延迟、端到端 MoE、迁移数据内容/数值量化正确性尚未建模。本次结果属于配置下的仿真输出，不是硬件测量或性能收益结论。gem5 源码和原有 AttAcc 实验保留。
