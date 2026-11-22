# AGENTS.md --- TokenStack-MoE 仿真工程协作规范

## 1. 项目目标

本工程服务于以下研究目标：

**基于 TokenStack 异构 HBM 拓扑，研究 GPU + HBM-PIM 架构下的 MoE
大语言模型 Decode 加速，以及 KV Cache 分层管理和预测式预取。**

当前研究重点包括：

1.  建立 TokenStack HBM Stack 的硬件模型。
2.  建模 Capacity Layer、Compute Layer 和 Logic Base Die。
3.  建模 PIM 计算、DRAM 访问以及层间 KV 数据迁移。
4.  建立 MoE Decode 工作负载和端到端性能模型。
5.  评估 TokenStack-KV、InfiniGen 以及 LightGBM/TCN 等预测式 KV
    管理方法。

开发时必须区分**硬件时序模型**、**系统性能模型**和**算法策略模型**，避免把它们混为一个未经验证的抽象模型。

------------------------------------------------------------------------

## 2. 已确定的仿真技术路线

本项目采用分层仿真方案，不以单一工具完成所有建模任务。

### 2.1 AttAcc + 修改版 Ramulator2：HBM/PIM 硬件仿真

这是当前优先建设的硬件仿真路径。

职责：

-   HBM Channel、Pseudo Channel、Bank Group、Bank 等 DRAM 组织。
-   DRAM 命令、时序约束、访问延迟和带宽。
-   Capacity Layer 的普通 DRAM 访问。
-   Compute Layer 的 PIM 扩展和计算时序。
-   PIM 命令、Bank/Bank Group 资源约束及相关竞争。
-   为层间数据迁移提供可测量的 DRAM 读写时延。

**开发原则：**

-   优先检查并复用 TokenStack 前身 AttAcc Simulator 中的代码及其修改版
    Ramulator2。
-   不要默认原版 Ramulator2 已经支持 AttAcc 所需的全部 PIM 命令和时序。
-   先复现 AttAcc 原有基线，再逐步扩展 TokenStack。
-   不要在没有检查源码、配置和测试的情况下，声称某个 PIM 功能已经存在。
-   对 Ramulator2 的修改应尽量局部化，并保留与上游版本的差异记录。

参考仓库： - AttAcc
Simulator：<https://github.com/scale-snu/attacc_simulator> -
Ramulator2：<https://github.com/CMU-SAFARI/ramulator2>

### 2.2 LLMSimulator：MoE 推理与系统性能仿真

LLMSimulator 负责更高层的推理工作负载和系统性能建模。

预期职责：

-   MoE 模型和 Decode 工作负载。
-   GPU 计算性能抽象。
-   Attention、Router、Expert 等阶段的性能建模。
-   KV Cache 生命周期、分层放置和管理策略。
-   请求调度、批处理及端到端性能统计。
-   调用或读取 Ramulator2 提供的内存/PIM 时序结果。

**开发原则：**

-   先确认当前 LLMSimulator 版本实际支持的模型、工作负载和 PIM
    模式，不得根据项目名称推断功能。
-   先建立可重复的 MoE baseline，再接入 TokenStack 优化。
-   初期优先采用 trace-driven
    或离线时序接口耦合，不要一开始就实现复杂的实时联合仿真。
-   统一请求 Trace
    的地址、大小、读写类型、发出时间、完成时间和来源标识。
-   任何简化的 GPU 性能模型都必须明确标注假设和适用范围。

参考仓库： - LLMSimulator：<https://github.com/scale-snu/LLMSimulator>

### 2.3 gem5：当前保留，后续按需引入

**当前工作区已经搭建 gem5 环境。必须保留现有 gem5
源码、编译产物、配置和用户修改。**

但 gem5 不是当前 TokenStack HBM 硬件建模的首选主平台。

当前原则：

-   不要因为 gem5 已经安装，就把 TokenStack 的 DRAM/PIM
    时序模型全部迁移到 gem5。
-   不要在当前阶段擅自开发完整的 `BaseDieController`、Compute Layer 或
    PIM DRAM 模型。
-   不要删除、覆盖或重建现有 gem5 环境。
-   只有当用户明确要求，或研究问题确实需要更细粒度的
    CPU、DMA、内存控制器、事件交互或系统级建模时，才提出并实施 gem5
    扩展。
-   如需使用
    gem5，先检查当前版本、构建目标、已有配置和本地修改，确保实现与该版本
    API 兼容。

------------------------------------------------------------------------

## 3. TokenStack 硬件建模约定

### 3.1 逻辑组成

建模时至少区分以下三个部分：

1.  **Capacity Layer**：普通 DRAM
    容量层，主要承载容量需求较大的数据，例如冷 KV
    Cache；其他模型数据的放置由实验配置决定。
2.  **Compute Layer**：带有 PIM 能力的 DRAM 层，可承载热 KV
    Cache，并执行经过验证适合近存执行的计算。
3.  **Logic Base
    Die**：负责控制、数据搬运和辅助功能。初期可以在系统前端/行为模型中抽象，不要求一开始实现
    RTL 级 Base Die。

Base Die 的候选功能包括：

-   Memory Controller / Request Arbitration
-   Address Translation / Layer Mapping
-   DMA Engine
-   SRAM Buffer
-   Quantization / Dequantization
-   PIM / Attention Coordination

不要把候选功能误写成已经实现的硬件模块。每个模块必须有明确的建模范围、参数、时序和测试。

### 3.2 数据通路

层间迁移应明确建模数据路径和资源占用。一个可用的初始抽象是：

`Source DRAM -> Base Die Buffer -> Target DRAM`

Promotion 和 Demotion 至少应分别记录：

-   源层和目标层。
-   迁移数据量及 Page/Block 粒度。
-   请求发出时间和完成时间。
-   DRAM 读写时间。
-   Buffer 占用及排队时间。
-   DMA/TSV 带宽约束。
-   量化/反量化时间（若该实验启用）。
-   与前台请求、后台迁移之间的资源竞争。

不要默认层间迁移是零开销，也不要让 Capacity 和 Compute 两个独立 DRAM
实例在没有共享资源仲裁的情况下，各自获得完整的 Stack 级带宽。

### 3.3 参数管理

所有关键硬件参数应集中配置，不要散落在代码常量中。至少考虑：

-   Stack 数量。
-   每 Stack 的 Channel / Pseudo Channel 组织。
-   DRAM Layer 数量及类型。
-   每层容量。
-   Bank Group / Bank 组织。
-   DRAM 时序参数。
-   PIM 单元位置、数量、精度和吞吐率。
-   Stack 内部数据通路带宽。
-   Base Die Buffer 容量。
-   DMA 并发度和队列深度。
-   Quantization / Dequantization 吞吐率。
-   KV Page / Block 大小。

论文中的**架构设计参数**与**具体实验平台参数**必须分开记录。若来源、单位或适用条件不明确，标记为
`TODO: verify`，不要自行补值。尤其不要把单层容量、单 Stack 容量和单 GPU
总容量混为一谈。

------------------------------------------------------------------------

## 4. 当前推荐开发顺序

按以下顺序推进，除非用户明确调整优先级。

### Phase 0：仓库与环境盘点

-   检查当前工作目录、Git 状态、分支和未提交修改。
-   记录 gem5 版本、构建目标和可执行文件位置。
-   不要假设 AttAcc、Ramulator2 或 LLMSimulator 已经安装。
-   不要覆盖现有文件或清理构建目录。
-   给出环境盘点结果和下一步计划。

### Phase 1：建立 AttAcc 基线

-   获取并检查 AttAcc 仓库及其子模块/依赖。
-   阅读 README、构建脚本、配置文件和运行入口。
-   按项目原有说明构建，不要先进行大规模代码修改。
-   运行最小示例，保存完整命令和日志。
-   确认修改版 Ramulator2 的来源、版本和 PIM 扩展。
-   记录可复现的基线结果。

### Phase 2：建立 TokenStack DRAM 拓扑

-   在 AttAcc/Ramulator2 现有能力上识别可复用的 DRAM 和 PIM 模型。
-   建立 Capacity 与 Compute 两类 Layer 的配置表达。
-   明确地址映射和请求路由。
-   对普通 DRAM 访问和 PIM 命令分别建立验证用例。
-   先实现最小单 Stack 模型，再扩展多 Stack。

### Phase 3：建立 Base Die 与迁移模型

-   先实现行为级模型，不急于 RTL。
-   实现 Promotion / Demotion 请求和队列。
-   建立 Buffer、DMA、共享带宽和量化开销模型。
-   用小规模 KV Page 测试验证数据量、延迟和带宽计算。
-   检查前台迁移与后台迁移的竞争关系。

### Phase 4：建立 LLMSimulator MoE Baseline

-   先运行原有支持的最小模型/工作负载。
-   确认 MoE Decode 阶段划分及统计口径。
-   获取 Attention、Router、Expert、KV 访问的时间或 Trace。
-   在没有优化的情况下保存基线。

### Phase 5：耦合硬件模型与系统模型

-   先使用离线 Trace 或文件接口。
-   统一地址空间、时间单位、请求粒度和统计定义。
-   将 Ramulator2 的 DRAM/PIM 延迟反馈给系统模型。
-   避免重复计算已包含在 Ramulator2 中的 DRAM 延迟。
-   验证请求数量、字节数、读写量和完成时间守恒。

### Phase 6：接入研究算法

按单变量、可归因的顺序接入：

1.  TokenStack 原始 KV 分层管理策略。
2.  Dense 场景的 InfiniGen 基线及其 MoE 迁移。
3.  预测式 KV Promotion。
4.  LightGBM / TCN 等预测器。
5.  容量管理、预取和迁移调度协同优化。

每个算法必须先有独立 baseline，再进行集成和消融实验。

### Phase 7：必要时引入 gem5

只有当研究问题需要更细粒度地研究
CPU、DMA、控制器、事件交互或系统级组件协同时，再评估 gem5。先给出引入
gem5 能回答的具体问题、所需模型和预期收益，再开始开发。

------------------------------------------------------------------------

## 5. 实验与结果规范

### 5.1 必须记录的指标

根据实验范围选择，不得把未建模的指标伪装成真实测量值。

**DRAM/PIM 层：**

-   Read / Write 请求数量和字节数。
-   平均/分位访问延迟（若工具支持）。
-   有效带宽和资源利用率。
-   Bank / Bank Group 访问分布。
-   Row hit / conflict（若模型提供）。
-   PIM 命令数量、执行周期和利用率。
-   PIM 与普通 DRAM 请求的竞争情况。

**迁移层：**

-   Promotion / Demotion 次数。
-   迁移字节数。
-   平均/分位迁移延迟。
-   有效迁移带宽。
-   Buffer 利用率和排队时间。
-   无效预取、重复迁移和迁移冲突。

**LLM 系统层：**

-   TTFT、TPOT。
-   Token throughput / Request throughput。
-   KV Cache 命中率。
-   GPU/PIM 工作量和等待时间。
-   Attention / Expert 时间占比。
-   端到端 Decode 延迟。

### 5.2 实验可复现性

每个实验应保存：

-   实验名称和目标。
-   Git commit / 分支。
-   软件版本和编译选项。
-   配置文件。
-   Workload、Trace 和随机种子。
-   完整运行命令。
-   原始日志和统计文件。
-   数据处理脚本。
-   结果说明和已知限制。

建议目录：

``` text
experiments/
  <experiment_name>/
    README.md
    config/
    traces/
    logs/
    stats/
    scripts/
    results/
```

不要覆盖已有实验结果。新实验使用独立目录或带版本号的输出目录。

------------------------------------------------------------------------

## 6. Codex 修改代码时的工作规范

### 6.1 修改前

1.  先阅读当前目录和相关源码，不要凭印象猜测项目结构。
2.  执行 `git status --short`，识别用户未提交的修改。
3.  检查项目版本、依赖和构建方式。
4.  先说明计划修改哪些文件，以及为什么。
5.  优先做最小范围修改，不进行无关重构。

### 6.2 修改中

-   保留用户已有代码和实验结果。
-   不擅自删除、重命名或覆盖文件。
-   不擅自升级依赖或切换分支。
-   不将伪代码、占位实现或理想化模型描述成已经完成的功能。
-   新增硬件参数必须有来源、单位和注释。
-   新增模型必须明确其精度：功能级、时序级、周期级或 RTL 级。
-   对不确定的硬件细节使用配置项或 TODO，并向用户说明，不要编造。
-   修改上游项目时尽量使用独立分支或补丁，保留原始基线。

### 6.3 修改后

1.  运行最小相关构建或测试。
2.  运行一个小规模 smoke test。
3.  检查输出是否包含预期统计。
4.  报告实际修改的文件。
5.  报告实际执行的命令和测试结果。
6.  明确列出未测试内容、假设和限制。

**严禁：**

-   没有运行实验却声称实验通过。
-   没有统计依据却声称获得性能提升。
-   将模型估算值称为真实硬件测量值。
-   混淆 GB/GiB、Gb/s/GB/s、ns/cycle 等单位。
-   在不同时间基准或不同统计口径之间直接比较结果。
-   未经用户同意清理、重置或覆盖现有 gem5 环境。

------------------------------------------------------------------------

## 7. 代码风格与接口约定

-   遵循所在子项目的既有代码风格，不强行统一不同仓库。
-   Python 代码优先使用类型标注、清晰的数据结构和可复现配置。
-   C++ 扩展遵循对应项目的编译规范和命名习惯。
-   Trace 格式应有版本号和字段说明。
-   时间单位在接口中显式声明，避免默认单位。
-   地址、长度、Layer ID、Channel ID 等字段应有明确类型和边界检查。
-   配置文件与实验结果分离。
-   关键模块提供小规模单元测试或可重复的验证脚本。

------------------------------------------------------------------------

## 8. 当前阶段的明确边界

当前已知状态：

-   当前工程已经搭建 gem5 环境。
-   AttAcc/Ramulator2 和 LLMSimulator 不应被假设为已安装或已配置。
-   当前优先任务是盘点现有工程，然后获取并运行 AttAcc 基线。
-   当前不优先在 gem5 中开发 TokenStack 硬件拓扑。
-   当前不优先实现完整 MoE 端到端系统。
-   当前不优先加入 InfiniGen、LightGBM 或 TCN。

**当前首要交付物：一份可复现的 AttAcc + 修改版 Ramulator2
环境，以及其最小 HBM/PIM 基线结果。**

完成该交付物后，再开始扩展 Capacity Layer、Compute Layer 和 Base Die
行为模型。

------------------------------------------------------------------------

## 9. 每次任务结束时的汇报格式

每次完成任务后，按以下格式汇报：

1.  **目标**：本次要解决的问题。
2.  **检查**：检查过的仓库、版本、配置和现有修改。
3.  **改动**：修改/新增的文件及用途。
4.  **实现**：实际实现的功能和建模精度。
5.  **验证**：执行的命令、测试和真实结果。
6.  **限制**：尚未实现的内容、假设和不确定参数。
7.  **下一步**：建议的最小后续任务。

如果任务范围不清楚，先检查并给出计划；如果存在会影响架构或数据正确性的关键歧义，先向用户提出一个最重要的澄清问题，不要自行做不可逆决定。
