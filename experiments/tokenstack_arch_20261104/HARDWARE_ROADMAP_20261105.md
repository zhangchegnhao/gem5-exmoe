# TokenStack 硬件仿真完成路线图

制定日期：2026-11-05。任务范围：在现有 AttAcc + 修改版 Ramulator2 上完成可复现的 TokenStack 硬件机制与时序仿真。研究对象是 Capacity / Compute / Logic Base Die 及其数据通路；保留已有 gem5 环境。

本路线图是后续工作计划，不表示下面的模块已经实现或实验已经通过。现状依据 `LAYERED.md`、当前源码及 `results/layered-domains-v3/verification.json`；论文依据固定为 arXiv:2605.05639v2，2026-08-12，文件和 SHA256 见 `references/tokenstack_source_20261105.json`。

## 1. 完成边界与现状

执行进展（2026-11-05）：已推进 H1-H5 的具名工程实现，并进入 H6 集成矩阵与离线发布验收，见 [当前实现与精度](HARDWARE.md)。结果入口为 `results/h6-engineering-v6/acceptance.json` 和 `results/h6-release-v3/manifest.json`，只有各文件实际标记 passed 才算对应工程检查通过。H0 的 Q1-Q5 论文证据门禁仍未关闭，因此下述严格论文完成判据尚未满足；不将工程假设配置通过标为论文忠实度验收通过。原路线图正文保留用于追溯。

“硬件搭建完成”的交付物是：参数和来源明确、拓扑可配置、DRAM/PIM 时序可追踪、迁移与前台共享实际资源、具有逐页流水和 attention 协调能力、支持论文实验规模拓扑的硬件仿真包。

论文 §7 将 Base Die 通过系统级时延、带宽共享和数据搬运效果建模，没有要求独立 RTL。因此本工程交付 DRAM 命令时序模型与 Base Die 行为/时序模型；不以 RTL、综合、流片或电路精度作为本轮终点。只读写/搬运部分做必要的功能数据校验，不实现完整 LLM 数值执行器。

本轮不接入完整 MoE/LLMSimulator、InfiniGen、LightGBM/TCN、运行时类别感知淘汰或选择性复制策略，不以论文端到端吞吐提升作为硬件正确性的验收条件。策略只作为固定 trace 的请求来源。跨卡 NVLink 保留可声明的外部接口边界，完整八卡 GPU/网络系统不属于硬件原型必做项。

已完成并继续保留：AttAcc 三种 PIM 基线；两个独立 DRAM/controller 时序域；普通访问/PIM 路由；真实迁移 RD/WR 回调；保守共享门控；小 trace 隔离和守恒检查。保存的 15 项测试与 273/822/2094 cycles 基线是既有结果，本次规划未重跑它们。

当前主要差距：两个时序域不能代表论文全部物理 Layer；两层共用 `HBM3_8Gb_2R`，Bank 组织未对齐图 3；只支持单 Channel/Stack、整页事务独占；64-cycle 切换间隔是合成假设；缺少静态 K/V 布局、页级映射、transpose、真实 page-buffer 流水、GPU 来源队列和完整结果就绪语义。

## 2. 依据与参数台账

每项工作、参数和默认值必须标记 `paper_arch`（论文架构）、`paper_eval`（论文实验平台）、`attacc_inherited`（沿用模型）、`engineering_assumption`（工程假设）或 `derived`（推导）。所有推导保留公式、单位和前提；未知字段保留 `TODO: verify`。

下表为此次核对得到的初始证据，H0 将其扩展为可机读台账；禁止把未核实项直接填入论文配置。

| 项目 | 原文定位 | 已核对内容 | 后续处理 |
| --- | --- | --- | --- |
| 垂直组织 | §4.1、图 3，PDF p4–5 | Base Die 在底部，Capacity 居中，Compute 在顶部 | 建立显式 die_id / layer_kind / vertical_order |
| 层容量 | §4.1、图 3 | 图示 Capacity 2 GiB/Layer、Compute 1 GiB/Layer；总容量 C×B_full+P×B_half | 不能把地址窗口或 preset 容量当成论文 Layer 容量 |
| Bank 差异 | 图 3(c)(d) | Capacity 16、Compute 8 banks/pseudo-channel；Compute 有 even/odd bank 路径、FP16 单元 | 核对与 §4.1“per-bank capacity halving”的口径；不能同时减 Bank 数和行数导致重复减半 |
| PIM 单元位置 | §4.1、图 3(d) | Bank Group 级描述，图示 HBM-PIM Unit、寄存器、FP16 ADD/MULT | 单元数、banks/unit、SIMD 宽度、吞吐和结果尾延迟仍需对照 AttAcc/原始引用 |
| Base Die MC | §4.2 | GPU/PIM 分离队列、Layer 地址转换、stack-local DMA | 当前 job 队列和两个地址区不足以覆盖这些机制 |
| DMA/Buffer | §4.2、§5.2 | page-at-a-time streaming，小型 SRAM page buffer per head group | 逐页流水是论文机制；当前整块独占需要替换 |
| K/V 布局 | §5.1、图 4 | K 按 token、V 按 feature 分布，分别让其归约 Bank-local | 布局、数据长度和实际 burst trace 必须一致 |
| 冷 KV 映射 | §5.2、式(4) | bank_cap(p)=p mod B_cap，文字同时描述跨 Capacity dies 分页交错 | 明确 B_cap 的 Layer/Channel/Bank 枚举；不能把 bank 等同于 die |
| 数据转换 | §5.2 | promotion 解量化、K scatter、V transpose；连续页读取和前页 transpose 可重叠；demotion 反向执行 | 拆成独立可验证流水阶段 |
| Buffer 公式 | §5.2 | 每 head group 的 FP16 K/V page buffer 为 T_page×d×2×2 B | T_page、d 和同时在途页数决定容量，16 KiB 不是固定论文值 |
| K8V4 | §6.2、§7 | K FP16→INT8、V FP16→INT4；转换吞吐配置为匹配 TSV，延迟隐藏在流水中 | 等量 K/V 且不含元数据时为 3/8；分别记录 K/V、元数据、padding |
| 迁移路径/调度 | §4.3、§6.4 | source→Base Die→target，两次 TSV；promotion 关键路径，demotion/fanout 背景填空闲 TSV 槽 | 来源优先级和按页仲裁有论文依据；具体公平算法、抢占粒度需要声明 |
| 外部路径 | §4.2–4.3 | UCIe D2D 连接 Base Die 与 GPU；跨 Stack 通过 GPU crossbar，跨卡经 NVLink | 不得虚构 Stack 直连或零开销远程路径 |
| 带宽 | 表 2、§7 | UCIe 512 GB/s/stack，TSV DMA 896 GB/s/stack，5 stacks/card | 属于实验设置；不能每个 Layer/Channel 各自重复获得整栈预算 |
| 基准拓扑 | 表 2、§8 拓扑敏感性 | 4 Capacity : 4 Compute 与 per-card Comp./Cap. 20/40 GB 点对应；扫描 cap1-comp7 至 cap7-comp1 | 先实现单栈 4+4，再扩展五栈；敏感性配置不等于在线策略 |
| 单位口径 | 图 3 对比表 2 | 图中 GiB/Layer，表中 per-card GB；按五栈和 4:4 推导有单位标签待核对 | 保留原文单位和字节口径，禁止默认二者等价；未解决则论文容量配置验收挂起 |
| Channel/时序 | 当前 AttAcc 源码/配置，论文未逐项列出 | 原型继承 HBM3 时序；原型 tCK=769 ps | Channel/PC 数、HBM3/HBM4 组织与时序依赖分别取证，不能把 HBM4 Base Die 概念直接换成 HBM4 DRAM 参数 |

纠正此前表述：“分块 DMA 完全属于工程扩展”过于笼统。论文明确要求按页 streaming；本路线图将其列为必做。任意更小 chunk 抢占、复杂乱序 DMA、多引擎并发仅在证据或实际验证需要时纳入。

## 3. 实施顺序与里程碑

依赖顺序：H0 → H1 → H2 → H3 → H4 → H5 → H6。每个里程碑保存独立实验目录、配置、输入、源码/补丁 hash、命令、原始日志、审计脚本和限制；通过验收后进入下一阶段。证据仍缺失时可用明确命名的实验假设配置推进独立开发，但不能把该配置记为论文配置已验收。

### H0：论文规格与参数冻结——下一项首先执行

任务：建立 `spec/paper_hardware_contract.md`、`spec/hardware_parameters.yaml` 和 `spec/open_questions.md`，每个字段包含来源分类、原文定位、单位、作用范围、当前值、验证方法和状态。特别解决图 3 Bank 数/容量减半口径、GB/GiB、Channel/PC 组织、PIM 单元共享范围、TSV 预算和两个 hop 的占用。给出资源连接图和关键数据流。

同时固定建模完成合同：哪些采用真实 Ramulator 命令时序、哪些采用行为模型、哪些仅有参数化时延。审计 AttAcc 的 MAC/SFM/MV/WRGB 等命令，明确 issue、数据可用、计算可用的区别；不以 command spacing 直接代替运算完成时延。

验收：每个必做模块和关键参数均能追溯；架构参数与实验平台参数分开；所有未公开参数有显式假设/边界和待核对项；形成可执行的单 Stack 4+4 规格，无相互矛盾的容量/地址公式。若关键歧义未解决，H0 只能部分完成，不能宣布论文规格冻结。

### H1：完整单 Stack 拓扑与静态数据布局

任务：将固定两个 domain 泛化为按 die_id 配置的 Capacity/Compute Layer 列表；Channel/PC/BG/Bank 与每 Layer 容量独立配置。先保留 1+1 调试 profile，再实现 4+4。每个物理 Layer 保持独立 Bank/row/refresh；只有经证明等效的组织才能合并。

建立统一地址描述：stack_id、die_id、layer_kind、channel、pseudo_channel、bank_group、bank、row、column、logical_block/page/head/K_or_V。实现逻辑块到物理页映射；Capacity 页交错、K token-major、V feature-interleaved，GPU 普通数据的 chunk-interleave 采用单独映射。大容量采用稀疏数据/元数据或地址计算，不分配整栈宿主机数组。

验收：跨 Layer/Channel/Bank 的首尾地址和随机往返无别名、无越界；总容量按规格守恒；Capacity 拒绝所有 PIM；K 的同 token 归约和 V 的同 feature 归约落在预期 Bank；页交错覆盖正确、非整除尾页有明确 padding。仅有普通读写通过不足以完成 H1。

### H2：按真实资源路径仲裁，替换整栈串行假设

任务：依据 H0 连接图区分 Channel/PC 命令总线、数据 burst、各 TSV 段、Base Die 端口和 Bank/PIM 单元；只共享实际共用的资源。增加 GPU、PIM、DMA 请求来源及独立队列，明确请求接受、资源保留、命令发出和数据就绪四个边界。

检查当前“DRAM nBL + 两个额外 hop”是否与选定物理路径重合，消除重复计时；对合法重叠保留流水并发。用实际 burst、读写 turnaround 和链路日历替代固定 64-cycle 整域等待；旧 guard 模式只作为保守对照。实现明确的优先级与公平保证，maintenance/refresh 不得无限期被前台阻挡。

验收：同一共享链路不超带宽、不重叠占用；独立 Channel 可并行，不能整栈逐请求串行；多 Layer 聚合不凭空获得倍增栈带宽；读写切换和未完成 PIM 依赖合法；队列压力下无丢请求/死锁/永久饥饿；refresh 在声明的预算内完成，无法证明时明确限制可支持的负载窗口。记录实际带宽与占用，不能把 grant cycles 直接称为利用率。

### H3：Base Die 逐页 DMA、Buffer、量化和 transpose 流水

任务：实现 Read → source hop → convert/layout transform → target hop → Write 的逐页依赖；按物理连接允许阶段重叠。每 head group 的 page buffer、在途页、DMA 描述符和队列均有实际分配/释放与背压。支持大于 Buffer 的逻辑块；页尚未写完不得更新为已驻留。

Promotion：读取 K8V4 页、解量化、K scatter、V transpose，写入 FP16 Compute 布局。Demotion：收集 FP16、反向布局转换、量化，再写 Capacity 页。K/V 元素数分开统计；记录 payload、量化元数据、burst padding 和源/目标物理字节。量化数值算法未公开时声明函数与误差验证边界，不声称复现论文模型精度。

以单个逐页引擎实现论文最低要求。引擎数量是配置项；若论文未给数量，可将单引擎和有限多引擎列为假设 profile 做敏感性。后台任务在页边界重新参与仲裁，等待中的关键 promotion 能获得准入，解决原型队列满导致的优先级反转。

验收：逻辑块大于 Buffer 时仍可完成，Buffer 峰值与块总长无关；每页数据/布局索引正确；转换后 K/V 字节和 padding 逐项可解释；转换带宽不低于 TSV 的论文实验设置可隐藏转换时延，限速设置应显现瓶颈；DMA/读/转换/写阶段的重叠不违反依赖；前台与背景均无永久饥饿。将预期有损量化与搬运/布局错误分别检查。

### H4：PIM 结果时序与 Attention Coordinator

任务：完成经过源码和证据核实的 PIM 单元位置、寄存器/缓冲、吞吐和资源占用；为每种支持的命令记录 `issue_cycle`、`operand_ready_cycle`、`result_ready_cycle`，必要时再记录结果交付。依赖命令与迁移只能在所需结果就绪后继续，不能在 MAC 最后发出时释放全部相关资源。

实现 coordinator 的 query/attention-weight broadcast、score/output gather，以及拓扑确实需要的 partial-reduce。根据 §5.1，单个 head 的 K/V Bank-local 归约不应被错误地变成全 Bank partial-sum 流量；跨 Head/Channel/Stack 的拼接或归约由具体分区决定。建立 attention trace 生成器，在多个 token/head/dimension 组合上验证它与 H1 布局一致；softmax 放置和执行资源从 AttAcc/论文取证后明确建模。

验收：PIM 与普通访问确实竞争被占用的 Bank/总线/寄存器资源；不允许结果未就绪就开始依赖操作；广播/收集元素和字节守恒，无额外虚构跨 Bank 归约；小矩阵的索引/收集顺序与 CPU 参考一致；结果完成时间可以与最后命令周期区分。参数缺失时采用有来源的估算/区间，不宣称硅后校准精度。

### H5：多 Channel、多 Stack 与主机接口

任务：从 1 Stack 扩展到 2 Stack，再到论文实验的 5 Stack/card profile；每栈内部拓扑沿用已验证规格。外部 GPU 来源使用轻量 trace 注入器，不引入完整 GPU 核心模型。Base Die UCIe ingress/egress 有独立端口、带宽、延迟和队列；来自各类 Layer 的普通访问都经过合法主机路径。

跨 Stack 请求通过声明的 GPU crossbar 路径返回目标栈，不实现虚构直连；GPU 转发的端口/队列竞争可参数化。表 2 的 512 GB/s UCIe/stack 和 896 GB/s TSV/stack 分别记账；未给出的交叉开关/协议延迟保留假设来源。跨卡边界保留 NVLink 路由/外部时延接口，并拒绝未配置的跨卡请求；完整八卡工作负载不是完成前提。

验收：跨栈无地址别名；每栈链路预算独立，经过同一主机端口的流量受聚合预算限制；远程访问按 source egress、host path、target ingress 分段守恒；背景迁移不能绕过主机路径偷取带宽；扩展到 5 Stack 后总容量按各层字节精确累计。不给出“不考虑瓶颈就线性扩展”的硬性性能结论。

### H6：集成验收、校准与可复现交付

任务：提供 `tokenstack_smoke`、`tokenstack_paper_profile`、旧 conservative profile 和开关对照；论文 profile 只接受已核对或明确声明假设的参数，报告中区分两类。提供一条干净环境构建/运行入口及完整增量补丁，避免依赖未归档的 untracked 源码。

验收矩阵至少覆盖：普通 RD/WR、PIM、row hit/conflict、同/异 Channel 竞争、K/V 布局、页交错、双向迁移、Buffer 背压、转换快/慢、前台/背景、GPU/PIM 队列、公平性、refresh、结果尾延迟、跨 Stack 路径，以及容量/地址/队列的无效输入。以固定种子生成随机混合 trace，并保留可定位到请求/页/资源的失败日志。

对单路径用可解释的 DRAM 时序与链路服务界限校验；对重叠执行核查资源日历，不把各请求排队延迟相加。校准依据来自论文公开数据、AttAcc 验证用例和有来源的组件参数；微基准自洽不等于测得真实硬件。

最终交付：规格/来源台账、完整拓扑图、集中 YAML、代码与补丁、trace/schema 文档、微基准与集成回归、请求/字节/时间/占用审计、已知限制和可重复运行的结果目录。论文未公开参数的残余不确定性附在交付中，不能将其隐藏为“已精确复现”。

## 4. 最终完成判据

只有同时满足以下条件，才将状态改为“TokenStack 硬件仿真搭建完成”：

1. 论文架构必需模块全部实现或以明示且验证的时序接口表达；拓扑、布局、page DMA、coordinator、主机边界均可追溯。
2. 单 Stack 完整规格和五 Stack 实验规模可运行，容量/地址/Bank 组织无已知关键矛盾；必需的未决单位/拓扑问题已经解决。
3. GPU、PIM、迁移、refresh 对实际共享资源的竞争合法；原型整块独占和固定 guard 不再是论文 profile 唯一执行路径。
4. 明确区分请求接受、命令发出、数据返回、计算结果就绪和迁移驻留提交；所有有依赖的操作满足正确完成边界。
5. Payload/元数据/padding、K/V 转换、请求/页 ID、源读/目标写、Buffer 生命周期和最终时间通过一致性审计。
6. 所有必需回归和负例通过；关闭新功能时原 AttAcc 基线保持；补丁链和全新环境最小复现通过，旧 gem5 环境保留。
7. 未公开的微架构常数有显式假设、敏感性区间和误差影响。完成的是有这些边界的硬件仿真，只有进一步证据充分时才称“论文硬件时序忠实复现”。

## 5. 本次交付与紧接着的任务

2026-11-05 执行记录：H0 已新增 `spec/paper_hardware_contract.md`、`spec/hardware_parameters.yaml`、`spec/open_questions.md`，以及 `scripts/audit_hardware_contract.py` 和负例测试；结果入口 `results/h0-contract-v1/`。这是 H0 部分完成，未达到论文规格冻结：Q1-Q5 仍 open。4+4 二进制容量工程候选已通过静态几何计算，但不是现有 Ramulator 可运行配置（每 Channel 1024/512 Mib 不在 refresh 表；默认 -1 索引须先防护）。接续 H1 的最小独立工作是显式 Layer 配置/地址模型及 refresh 参数安全路径，所有工程选择继续标记 assumption，不绕过论文冻结门禁。以下保留制定路线图时的交付记录。

本次新增路线图和固定论文参考文件/来源记录，并更新 README 入口；不修改仿真器、不重跑硬件实验，也不将计划中的功能计为完成。

下一项执行 H0：输出论文—参数—源码—测试对照表，优先解决图 3 的 Bank/容量口径、GB/GiB、Channel/PC 与共享 TSV 路径；随后据此设计 H1 的显式 Layer 列表。先完成这项，再推进 DMA，可避免在错误拓扑上继续增加调度功能。
