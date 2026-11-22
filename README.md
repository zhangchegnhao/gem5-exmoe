# gem5-exmoe：TokenStack / MoE 仿真工程

当前主要路径是 **AttAcc + 修改版 Ramulator2**，用于 TokenStack Capacity/Compute Layer、Base Die 迁移与共享资源时序；GPU 使用 A100a 算力参数和五栈内存的行为模型。当前不是完整 GPU 微架构仿真，也尚未完成完整 MoE baseline 或 GPU 实机校准。

- [版本管理、获取与离线复现](docs/GIT_WORKFLOW.md)
- [TokenStack 硬件说明](experiments/tokenstack_arch_20261104/HARDWARE.md)
- [A100a + 五栈系统模型](experiments/tokenstack_arch_20261104/GPU_SYSTEM.md)
- [最新统一验收报告](experiments/tokenstack_arch_20261104/results/tsv-gpu-acceptance-20261118-v1/README.md)
- [离线发布与复现](experiments/tokenstack_arch_20261104/results/tsv-gpu-release-20261118-v1/REPRODUCE.md)
- [工程协作规范](AGENTS.md)

已保存的验收为 62 项测试、15 个原硬件场景、8 个 TSV 吞吐场景和 6 个 GPU 系统场景；论文 Q1–Q5 仍未冻结。具体状态以相应结果 manifest 为准。以下保留原 gem5 环境与入门实验说明。

# gem5 实验环境

此工作区固定使用 gem5 **v24.1.0.3**，源码提交为
`b9da2bfe1e21f6efa2730edd4e80e58c5d965458`。这是可复现的固定版本，
不代表最新发布版本。安装方式为本地源码编译，没有修改系统 Python。

## 目录

```text
gem5-src/                   上游源码及 build/X86/gem5.opt
build-x86.log               首次编译日志
environment.txt             机器、工具链和源码版本信息
experiments/
  configs/x86_se.py          单核 x86 SE + 两级经典缓存配置
  workloads/                hello.c 和 INT32 matmul.c
  bin/                      静态链接的工作负载
  scripts/                  编译、运行和统计提取脚本
  results/                  每次实验独立目录，含 config.ini/json、stats.txt
```

## 安装验证（2026-11-04）

源码编译和动态库检查通过。下列 6 次模拟均正常退出，输出与本机执行一致，
且生成了非空的 `stats.txt`、`config.ini` 和 `config.json`：

| CPU 模式 | hello | matmul 32×32 |
| --- | --- | --- |
| timing | 通过 | 通过，checksum=6 |
| atomic | 通过 | 通过，checksum=6 |
| o3 | 通过 | 通过，checksum=6 |

结果路径见 `verification.json`，运行日志见 `verify-*.log`。
`gem5-build-info.txt` 保存模拟器的实际构建信息。

## 开始运行

在终端中执行：

```bash
cd /home/ubuntu22/gem5
./experiments/scripts/run_demo.sh
```

也可执行 `source /home/ubuntu22/gem5/env.sh`，在当前 Bash 终端中启用
`gem5.opt` 命令以及 `GEM5_ROOT`、`GEM5_EXPERIMENTS` 环境变量。
它不会修改 `~/.bashrc`。

该脚本会编译两个工作负载，先本机执行，再在 gem5 中执行，核对输出，
最后展示部分统计项。默认使用 `TimingSimpleCPU`，统计覆盖整个进程，
包括 C 运行库初始化、矩阵初始化、计算和输出。

其他 CPU：

```bash
./experiments/scripts/run_demo.sh atomic  # 快速功能验证，非详细时序
./experiments/scripts/run_demo.sh o3      # 乱序 CPU，模拟较慢
```

三种配置的缓存与内存容量相同。O3CPU 使用该 release 默认微体系结构参数，
不对应经过校准的某款商用 CPU。Atomic 模式的性能数字不能当作详细时序结果。

## 自定义运行

下面运行 64×64 矩阵乘法，并把 L2 改成 512 KiB：

```bash
cd /home/ubuntu22/gem5
./gem5-src/build/X86/gem5.opt --outdir=experiments/results/matmul64-l2-512k \
    experiments/configs/x86_se.py --cpu timing --l2 512KiB \
    experiments/bin/matmul 64
```

配置参数必须放在被模拟程序路径之前；程序路径后面是工作负载参数。
gem5 自身的 `--outdir` 必须放在 Python 配置脚本之前。
每次实验请使用新的输出目录，避免覆盖。

默认系统：

| 项目 | 配置 |
| --- | --- |
| 模式 | SE 系统调用仿真，无完整操作系统 |
| CPU | 1 核 X86TimingSimpleCPU，1 GHz |
| L1I / L1D | 各 32 KiB，2 路 |
| L2 | 256 KiB，8 路 |
| 缓存行 | 64 B |
| 主存 | DDR3_1600_8x8，8 GiB 模拟容量 |

8 GiB 是被模拟系统的地址空间容量，不是在宿主机立即预分配 8 GiB 内存。
缓存延迟等参数在 `experiments/configs/x86_se.py` 中；
每次运行的 `config.ini` 和 `config.json` 是实际实例化配置的记录。

查看统计：

```bash
python3 experiments/scripts/summarize.py \
    experiments/results/matmul64-l2-512k/stats.txt
```

`simSeconds` 是模拟系统时间，`hostSeconds` 是宿主机实际运行用时。
统计项名称随模型变化，完整信息以原始 `stats.txt` 为准。

## 修改源码后重新编译

```bash
cd /home/ubuntu22/gem5
./experiments/scripts/build_gem5.sh
```

默认使用 2 个编译任务和 GNU gold 链接器，以控制内存使用。
资源充足时可设置 `JOBS=4 ./experiments/scripts/build_gem5.sh`。
直接构建的等价命令是：

```bash
cd /home/ubuntu22/gem5/gem5-src
scons build/X86/gem5.opt -j2 --linker=gold
```

源码是浅克隆，位于 release 标签对应的 detached HEAD。
开始修改 CIM 模型之前可在源码目录执行 `git switch -c cim-research`，
建立自己的研究分支。学习实验文件位于源码仓库外，便于保留原始上游版本。

## 当前范围与后续研究

- 已准备 x86 SE 入门环境，不包含 Linux 全系统镜像或 GPU 模拟环境。
- 未安装的可选开发库使 PNG 帧缓冲、HDF5 输出和 Capstone 反汇编功能关闭；
  不影响本环境的 SE CPU/缓存/DDR 模拟和文本统计。
- 未安装 pydot，暂不自动绘制系统拓扑图；`config.ini/json` 正常生成。
- `matmul.c` 是环境验证程序：固定最大行跨度 256，INT32、单线程，
  不代表经过优化的 GEMM，也不是完整 LLM 基准。
- 后续性能研究应加入明确的 ROI（感兴趣区间）、预热、工作负载规模和精度说明。
- 本环境尚未实现 CIM 阵列、数据搬运协议或能耗模型；这些需要按课题设计扩展。

参考：[gem5 文档](https://www.gem5.org/documentation/)、
[Learning gem5](https://www.gem5.org/documentation/learning_gem5/introduction/)。
