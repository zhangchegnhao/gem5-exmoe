# H0 论文证据复核 v2

本目录固定本地 `arXiv:2605.05639v2` 的 PDF 哈希、`pdftotext -layout` 提取文本、行号摘录和 Q1–Q5 证据矩阵。

结论：论文明确给出三层拓扑、2/1 GiB 层容量、图示 16/8 banks per pseudo-channel、两跳 TSV 迁移，以及 896 GB/s TSV 和 512 GB/s/stack UCIe 评估带宽；但 Q1–Q5 的关键微架构歧义仍未由论文唯一确定。该矩阵用于参数校准，不改变工程运行配置，也不关闭论文冻结门禁。

复核命令：

```bash
pdftotext -layout references/tokenstack_arxiv_2605.05639v2.pdf results/h0-evidence-v2/paper-text-layout.txt
PYTHONDONTWRITEBYTECODE=1 python3 experiments/tokenstack_arch_20261104/scripts/audit_hardware_contract.py h0-contract-audit-v2
PYTHONDONTWRITEBYTECODE=1 python3 experiments/tokenstack_arch_20261104/scripts/audit_hardware_contract.py h0-paper-freeze-v2 --require-paper-freeze
```

敏感性记录见 `engineering-sensitivity.json`：覆盖转换吞吐、PIM result-ready 尾延迟、合成 refresh 压力和跨 Stack 主机带宽，并保留迁移字节/请求守恒与 AttAcc 基线。
