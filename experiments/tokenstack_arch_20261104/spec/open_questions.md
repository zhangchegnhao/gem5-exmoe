# H0 未决事项与有界选择

2026-11-05；固定证据版本为 arXiv:2605.05639v2。下列均未解决，禁止把“选择工程候选”改写成“已确认论文”。本地证据不足的项保留 TODO: verify，不据常识补全。参数 ID 和问题状态同时保存在 `hardware_parameters.yaml`。

实施更新：H1 已修复 Q3 中识别的 -1 索引风险，未知 density 现在要求显式 nRFC/nREFISB，非法配置会拒绝；H4 在 Fabric 包装层增加可扫参 result-ready。工程实现和测试见 `../HARDWARE.md`，并未将这些本地假设当作关闭 Q3/Q5 的论文证据。表格中的风险发现和推进建议保留为 H0 历史记录。

| ID | 问题、证据及影响 | 有界选择 / 独立工作 | 关闭所需证据 |
| --- | --- | --- | --- |
| Q1（关键） | 图 3 是 Capacity 16、Compute 8 Bank/PC，容量 2/1 GiB；§4.1 又说 per-bank storage halving。若 Bank 和每 Bank 容量同时减半，总容量将成为 1/4，冲突。 | 候选采用图示 Bank 数、同容量 Bank，仅减一次；保留正文解释为备选，不混合。可推进可配置 Bank 几何，论文 Bank/容量规格冻结挂起。 | 作者公开硬件配置/源码或勘误，明确 Bank、sub-bank、PIM unit 以及“per-bank”粒度；给出能同时满足总容量与 Bank 数的公式。 |
| Q2（关键） | 图 3 GiB 与表 2 per-card GB；4+4、5 Stack 算得 40/20 GiB，而不是十进制 40/20 GB。两种 byte 总数相差 7.3741824%。 | 主工程候选固定二进制；另列十进制容量备选，禁止直接比较容量敏感实验。 | 论文原始运行配置/作者说明或勘误；明确 byte 常量、权重预留前后口径。未解决前不能声称表 2 容量精确复现。 |
| Q3（关键） | 论文未逐项给每物理 die 的 Channel/PC/rank/rows；AttAcc 的 2R preset 是 stack 组织，不能每 die 再嵌一个完整 stack。 | 候选 16CH×2PC×1rank×4BG，按 2/1 GiB 推导 rows=4096；这是工程选择。每 Channel 1024/512 Mib 超出当前 refresh 表。H1 首先增加明确参数/边界检查路径，未支持配置必须拒绝，禁止触发 -1 数组索引。 | 有来源的实际 die 几何/refresh 时序；或声明假设 profile，记录敏感性与适用范围。后者可允许运行但不等于论文事实。 |
| Q4（关键） | §4.3 明确两 hop 和栈共享，表 2 给 896 GB/s；没有逐段 TSV 拓扑、全双工、读写切换和与 DRAM nBL 覆盖关系。 | 候选保守单聚合预算，source/target 物理字节都扣账；暂不据此声称物理串行。H2 资源图必须标明“DRAM burst 已覆盖/额外区段”，旧 guard 模式保留对照。 | 物理数据通路/总线参数或公开实现，明确每段带宽、方向共享、时钟以及 nBL 到 TSV 的对应。用区段日历验证不重计、不超预算。 |
| Q5（关键） | 图 3(d) 的 BG/Unit0/1/even-odd 不足以唯一确定 units/BG、共享 Bank、SIMD、流水尾延迟；AttAcc 有三种放置，MAC/SFM 等回调在 issue。表 2 还写 bank-level PIM。 | 不从 6-cycle MAC 间距推导完成；H4 先建立 issue/result-ready 两个事件和显式参数。PIM bytes 未知不报告成测量值 0。 | 核对引用的 AttAcc 实现/原论文与可公开 TokenStack 配置；分别确定 MAC、move、softmax 延迟、寄存器依赖和结果路径。未公开常数只能作为有来源假设/区间。 |
| Q6（后续必需） | §5.2 给 T_page*d*2*2 B，§7 workload 有 16-token logical KV block，但不能直接证明 DMA T_page=16；head group、量化分组、scale 元数据和 transpose 吞吐未完整给定。 | H3 把页、逻辑块和 head group 分开配置；3/8 只对等量 K/V payload 成立。可先验证无损索引/byte 生命周期；量化函数单列假设，不声称复现精度损失。 | 明确 page/group 大小、scale/zero-point 格式、舍入/截断、尾页 padding 与转换吞吐。未公开时发布具名假设及数值误差边界。 |

H0 发布门禁：Q1-Q5 任一 open，`paper_spec_frozen` 必须 false。可以完成证据整理、静态维度审计和工程候选准备；不能把 H0 标为论文规格已完全冻结。本轮不联系作者、不修改上游依赖，也不让未知项阻止可逆的独立开发。

补充边界：图 3 画出的 Base Die 模块与 §4.2 描述作为机制合同；§7 是系统效果建模，并非 RTL 证据。896 GB/s 引用的 HBM3 产品、HBM4 Base Die 概念和 HBM3 评估不等于已有一组可直接互换的 DRAM 时序参数。五栈/卡与八 GPU 系统规模也不等于本地已实现或已校准。
