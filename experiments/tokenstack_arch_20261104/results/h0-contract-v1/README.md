# H0 证据与维度审计结果

日期：2026-11-05。审计对象是 39 项参数、固定论文 hash、现有 DRAM/mapper/controller/scheduler 源码和双域配置。本目录不是新增硬件性能微基准，不包含性能提升结论。

实际执行命令（工作目录 `/home/ubuntu22/gem5`，设置 `PYTHONDONTWRITEBYTECODE=1`）：

```
python3 experiments/tokenstack_arch_20261104/scripts/audit_hardware_contract.py h0-contract-v1
python3 -m unittest -v experiments/tokenstack_arch_20261104/tests/test_hardware_contract.py
python3 -m unittest -v experiments/tokenstack_arch_20261104/tests/test_layered.py experiments/tokenstack_arch_20261104/tests/test_closed_loop.py
python3 experiments/tokenstack_arch_20261104/scripts/audit_hardware_contract.py h0-freeze-gate-v1 --require-paper-freeze
```

结果：静态审计 12 项检查通过、39 项参数有来源/单位/范围；新增测试最终 9/9 通过（`hardware-contract-tests.log`）。已有闭环/分层测试本轮 7/7 通过，原始终端输出未归档，结果摘要记录于 `validation.json`。要求论文冻结的独立门禁按预期返回 2，结果保存在相邻 `h0-freeze-gate-v1/`。不重跑已存在的 273/822/2094 cycles 原 AttAcc 基线。

审计发现：

- 现有单 Channel、2-rank domain 解码容量为 1 GiB，每 PC 跨 rank 共 32 Bank；1 MiB region 仅是访问窗口。
- 工程候选每 die 为 2/1 GiB，单栈 4+4 为 8/4 GiB；五栈为 40/20 GiB。与表 2 十进制字面值 40/20 GB 存在 7.3741824% 的 byte 差异。
- 候选每 Channel 为 1024/512 Mib，不在既有 refresh density switch 中。默认 -1 索引先于 nRFC 参数覆写使用，H1 必须先实现安全路径；本轮没有执行该不支持配置。
- 实际 tCK 为 769 ps，覆盖 preset 末尾的 1300。正常 RD 回调距 issue 21 cycles，closed-loop WR 写恢复边界 29 cycles；PIM 仍无独立 result-ready 事件。
- Q1-Q5 仍 open，`paper_spec_frozen=false`；工程几何算式自洽不等于现有 runtime 已支持。

`audit.json` 保存检查、推导和关键源文件 SHA256；`source_snapshot/` 保存受审源码文本；`spec/`、`config/`、`scripts/`、`tests/` 保存输入和审计快照；`environment.json` 保存三仓库 HEAD/status、binary/library/CMakeCache hash。PDF 只保留既有固定引用，不在每个结果目录重复复制。

本轮未修改仿真器源码或构建。可执行文件及动态库 hash 与 `layered-domains-v3/metadata.json` 相同；gem5-src HEAD 为 `b9da2bfe1e21f6efa2730edd4e80e58c5d965458`、工作树 clean。AttAcc/Ramulator 既有修改保留。

接续最小任务：H1 显式 Layer 配置、静态地址/Bank/KV 布局，以及 refresh 参数校验和来源管理；以具名工程候选推进，论文参数歧义持续保留。
