# H6 最终工程验收记录

验收日期：2026-11-05。H1–H6 的声明工程配置已通过实现、集成与离线干净构建验收；**严格论文忠实度验收未通过**。H0 的 Q1–Q5 证据问题继续 open，不将工程假设视为论文规格冻结。

## 权威结果入口

| 内容 | 文件 | 实际结果 |
| --- | --- | --- |
| 工程集成矩阵 | `../h6-engineering-v6/acceptance.json` | 15 个场景 passed，55 项测试通过 |
| 原 AttAcc 回归 | `../h6-engineering-v6-attacc/regression.json` | bank / bg / buffer = 273 / 822 / 2094 cycles，统计一致 |
| 离线重建、编译和回归 | `manifest.json`、`clean-*.log` | `clean_build_passed: true`；重新构建后 55 项测试通过，三组基线一致 |
| 严格论文门禁 | `../h6-paper-freeze-gate-v1/audit.json`、`audit.log` | 预期拒绝；Q1–Q5 未关闭 |
| 交付后复现入口 | `REPRODUCE.md`、`reproduce_release.py` | 完整源码、补丁、依赖与实验工具归档，无需网络 clone |

早期 v1–v5 工程结果、release-v1/v2 均保留追溯，以本记录列出的最终目录为准。release-v1 的旧 BG 回归失败未被计为通过；修正后的 legacy 路径保持原统计，新物理 die 路径显式启用独立检查。

## 已验收的工程范围

- H1：显式 4 Capacity + 4 Compute dies/stack、集中配置、稀疏地址和 K/V 静态布局；每栈 12 GiB，五栈 60 GiB，采用明确的二进制容量候选。
- H2：实际 DRAM 命令发出前仲裁 Channel/PC、TSV 和相关资源；GPU、PIM、promotion、demotion 来源队列共享资源日历，保留 refresh 和结果就绪约束。
- H3：逐页 DMA、有限 SRAM、背压、转换和布局阶段；逻辑块大于 Buffer 时可以流水完成，分别审计逻辑 payload、元数据、padding 和实际 burst 字节。
- H4：attention 请求生成、broadcast/gather、PIM 结果事件及依赖；功能数值参考与时序模型分开。
- H5：1/2/5 Stack、UCIe 和 GPU crossbar 路径；未配置的跨卡请求拒绝，无虚构 Stack 直连。
- H6：15 场景矩阵、55 测试、负例、固定种子混合压力、原 AttAcc 回归、离线完整补丁和干净构建。

37-token promotion 用例：10 页，234 请求，源读 1184 B、目标写 6304 B，Buffer 峰值 512 B，任务完成 881 cycles。读写字节不同来自压缩格式、量化元数据、burst padding 和 hot scatter，并非字节丢失。每项流量按对应物理格式核对。

## 实际执行的最终命令

在 `/home/ubuntu22/gem5` 执行：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 experiments/tokenstack_arch_20261104/scripts/run_acceptance.py h6-engineering-v6 --profile tokenstack_paper_profile --allow-engineering-assumptions
PYTHONDONTWRITEBYTECODE=1 python3 experiments/tokenstack_arch_20261104/scripts/release_hardware.py h6-release-v3 --clean-build
```

第二条命令在新临时工作区恢复归档源码、应用完整补丁、加载本地依赖，以 GCC 12 / C++20 / Release 离线编译，再执行 smoke、全部 unittest 和三组 AttAcc 回归。150 个 Ramulator 源文件恢复后逐项哈希一致。完成后复核当前源码、4 个归档包和归档中 109 个工具/输入文件均无哈希差异；工程快照的 48 项文件哈希也与当前文件一致。

`clean-tests.log`：55 tests / OK（6.900 s）。干净构建 smoke：36 请求，读 512 B、写 512 B、TSV 1024 B，完成 129 cycles。完整 15 场景矩阵来自工程工作区；干净构建执行 smoke、55 测试和旧基线，未另行重复运行完整 15 场景矩阵。独立 `reproduce_release.py` 入口尚未单独运行，其等价的恢复/构建流程已由 release 脚本实际执行。

## 验收边界与后续

这是 DRAM 命令时序 + Base Die 行为/时序模型，并附独立功能参考；不是 RTL、真实硬件测量或完整 LLM 数值执行器。refresh、PIM 结果尾延迟、部分共享资源规则和 crossbar 参数含未校准工程假设。有限测试窗口的活性通过，不代表无限对抗到达的形式化无饥饿证明。

Q1 Bank/容量口径、Q2 GB/GiB、Q3 物理 DRAM 组织/refresh、Q4 TSV 物理共享边界、Q5 PIM 单元和结果时序仍阻止严格论文完成判据。下一项是补证与校准，依据见 `../../spec/open_questions.md`。未开展 MoE/LLMSimulator 或预测器集成。

gem5 源码保持 clean，HEAD 为 `b9da2bfe1e21f6efa2730edd4e80e58c5d965458`，既有 gem5 环境未重建或清理。
