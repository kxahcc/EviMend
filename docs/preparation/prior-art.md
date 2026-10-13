# 相关研究证据

状态：当前参考证据。最后维护：2026-10-13。
主要来源查阅日期：2026-09-12；2026-09-16 定点复核 mini-swe-agent 仓库 README、Agentless 与 TraceRepair 摘要及相关工程文档。2026-10-02–03 定点查阅 ACE、SkillRevise 和 SWE-Skills-Bench 的原文；2026-10-11 定点查阅 Anthropic、Deep Agents 上下文/记忆文档及 ACE v1 方法、v3 摘要页。具体版本/范围见下节及 sources.json；这些是不同范围的采集，不刷新旧证据，也不是完整查新。当前架构决策以 [开发设计草案](../development/system-design-draft.md) 为准，本文只记录来源、参考价值与复用限制。

采用“在成熟方法上升级”的定位。以下是面向技术决策的检索，不是系统综述或完整查新证明；录用状态若来自 arXiv Comments，明确视为作者页面声明。

## 优先使用和比较的工作

| 工作 / 来源状态 | 已覆盖的内容 | 参考价值与限制 |
|---|---|---|
| [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)，开源系统；MIT | 简单修复循环、模型和执行环境分离 | 可参考其模型/执行环境分离；实际采用前须固定提交并验收 |
| [SWE-agent](https://arxiv.org/abs/2405.15793)，2024 方法；[官方代码](https://github.com/SWE-agent/SWE-agent) 标注 NeurIPS 2024 | 面向 Agent 的仓库操作与执行接口 | 专用工具需求较强时的备选；现有工具设计是借鉴基础 |
| [Agentless](https://arxiv.org/abs/2407.01489)，2024 方法 | 分阶段定位、修复、补丁验证 | 简单且有竞争力的流程对照；不以复杂程度判优 |
| [SHERLOC](https://arxiv.org/html/2606.24820v2)，2026 方法；作者页面标注 EMNLP 2026 Main | 结构化诊断交接、定位与修复效果/成本、质量过滤 | 最接近的诊断增强参考；区分可部署运行和事后分析 |
| [ARISE](https://arxiv.org/html/2605.03117v2)，2026 预印本与工具 | 仓库图、语句级数据流切片、跨框架工具接入 | 有数据流瓶颈时才引入，先用轻量检索；避免提前搭大图系统 |
| [AgentFL](https://arxiv.org/abs/2403.16362)，2024/2025 版本，未在本轮核实正式发表条目 | 项目级定位与测试行为跟踪 | 防止把“仓库级定位”本身描述成新问题 |
| [TraceRepair](https://arxiv.org/html/2604.02647v2)，作者页面标注 ASE 2026 | 运行探针、不同修复策略及基于证据的交叉检查 | 借鉴探针与证据；正式比较需对齐定位条件 |
| [DebugRepair](https://arxiv.org/html/2604.19305v1)，2026 预印本 | 定向运行状态收集和迭代修复 | 动态诊断近邻；不直接比较不同已知位置条件下的成功数 |
| [执行 Trace 的 APR 研究](https://aclanthology.org/2025.knowledgenlp-1.17/)，2025 workshop 方法/实证 | 直接增加 Trace 的收益不稳定，复杂度影响有效性 | 支持优先筛选和压缩证据，而不是无限增加上下文 |
| [SkillRevise](https://arxiv.org/html/2606.01139v4)，作者页面标注 Findings of EMNLP 2026 | 诊断与保留约束、带迁移条件的原则记忆、绑定后修订并重执行选择 | 经验/Skill 机制的重要近邻；已有覆盖不能改名当新增机制 |
| [ACE](https://arxiv.org/html/2510.04618v3)，预印本 | 轨迹反思、增量经验维护与上下文更新 | 非参数学习的机制参考；原设置不提供本项目收益保证 |
| [SWE-Skills-Bench](https://arxiv.org/html/2603.15401v1)，本次查看 v1 预印本 | 软件任务中 Skill 适配、收益差异与上下文干扰 | 说明需检查迁移边界；其需求驱动任务与日志故障修复不同 |
| [Socratic-SWE](https://arxiv.org/abs/2606.07412)，2026 预印本，页面标注 under review | 轨迹提炼技能，再引导训练任务与模型学习 | 中后期参考；不作为首轮必要依赖 |
| [SWE-Gym](https://arxiv.org/abs/2412.21139)，作者页面标注 ICML 2025；[SWE-smith](https://arxiv.org/abs/2504.21798)，2025 预印本 | 可执行训练环境、轨迹和任务生成 | 真正启动训练前再评估；训练并非无环境依赖的捷径 |

## 当前 Agent 机制的借鉴与待验证差别

[mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) 展示轻量工具循环与线性轨迹；本次查看的主分支 README 标明 v2，未安装或固定 SHA，不能直接沿用旧代码接口假设。[Agentless](https://arxiv.org/abs/2407.01489) 是简化定位、修复和验证流程的参考；这些公开表现不外推到本项目。

[TraceRepair 摘要](https://arxiv.org/abs/2604.02647) 明确以运行证据约束修复假设，并使用探针与多策略 Agent。因此“加入约束/运行探针”已经存在，不能独立声称首创。

我们的设计推断是：将业务保持义务绑定到诊断、补丁和反例，并仅对会改变修复方向的证据缺口追加检查，可能在有限执行预算下减少错误修改与无效运行。这个差别需要进一步全文/实现比较及同预算实验，不是已证实的研究空白；单 Agent 的工程选择也不是独立创新证明。具体机制只在开发设计维护。

## 两个会改变实施决策的细节

SHERLOC 的质量过滤分析部分使用了正确补丁辅助评判；其无补丁验证器被评估的是与回顾性质量标签的一致性，论文注明尚未评估该选择器驱动的下游修复。因而“低额外预算、无正确补丁、真实执行全部分支”的增量实验具有明确比较对象。这是候选升级切口，不是已经证明的独占创新。[论文限制与验证分析](https://arxiv.org/html/2606.24820v2)

ARISE 明确报告了额外 Token 消耗，图构建时间也随仓库规模变化。对成本评估的启示是：先验证数据流信息是否能挽回失败，再决定是否承担图构建成本；一次一仓的比赛场景还应计入首次建图成本。[成本分析](https://arxiv.org/html/2605.03117v2)

诊断测试选择和信息增益也不是新概念。2010 年 QSIC 工作已研究面向定位的测试优先级，2011 年存在相关期刊扩展。因此不把“按信息价值选择检查”作为独立首创；关注面向当前日志—代码—修复链路的具体实现与新增证据。[QSIC 2010 元数据](https://dblp.org/rec/conf/qsic/Gonzalez-SanchezPGG10)、[作者机构的期刊记录](https://research.tudelft.nl/en/publications/prioritizing-tests-for-software-fault-localization-2)

## 上下文、经验与 Skills 的定点查阅

查阅日期为 2026-10-02–03。ACE 查看 v3 方法、实验设置/对照与限制；SkillRevise 查看 v1 方法、评测/消融及失败限制，并在 10 月 3 日复核 v4 引言和方法；SWE-Skills-Bench 查看 v1 摘要、方法、实验条件与版本干扰案例。不将这次定点阅读称为最新全文综述或原方法复现。

ACE 的结构化经验维护可作为复用基础。[ACE 方法](https://arxiv.org/html/2510.04618v3)

SkillRevise 已覆盖保留约束、迁移限制、证据绑定和有界修订选择，v4 也包含软件任务。不能凭“记忆＋验证＋Skill 更新”或软件领域定位声称独占新颖性。[SkillRevise v4](https://arxiv.org/html/2606.01139v4)

SWE-Skills-Bench 提供 Skill 与项目约定不匹配而造成损害的案例；本项目仍须自己验证这些风险及收益，不能外推其成绩。[SWE-Skills-Bench](https://arxiv.org/html/2603.15401v1)

据此的设计推断是：可在成熟机制上升级，把当前业务义务和前提证据用于每一步修复控制，决定迁移修法、返回区分问题还是避开不适用经验，并用新反例修订迁移边界。此控制目标与冷启动修订 Skill 有差别，但不证明近邻无法适配。需要同知识静态 Skill、普通检索、关键机制消融及合理近邻适配，检验独立案例上的修复、负迁移和总成本；具体设计及比较归属仍在设计/评测文档。

## 上下文组织与存储的成熟设计依据

本节首次采集日期为 2026-10-11；2026-10-13 定点复核 Anthropic 上下文文章、Deep Agents 上下文官网、mini-swe-agent 官网与 ACE v1 方法，不刷新其他来源的采集日期，也没有做新颖性穷尽检索。Anthropic 的工程文章讨论外置原件和轻量引用、渐进读取、结构化笔记与压缩，并指出过度压缩可能丢失后来才显重要的内容。可借鉴“完整材料留在外部、当前决策构建工作集”，不能把文件存储本身描述为研究创新。[官方文章](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)

Deep Agents 文档分别说明常驻 memory、按需 Skills、运行状态以及内容卸载/历史摘要；memory 文档区分任务内状态和跨线程持久记忆。它们支持优先复用框架与现有 backend，而不是另造图数据库和长期记忆服务。10月13日同时只读核对本机锁定 Deep Agents 0.7.18 的 graph/middleware：框架确有摘要模块，但项目明确排除它并关闭卸载阈值；原生接口存在不表示本项目已启用或完成兼容接线。在线文档与本机检查均不提供当前修复收益证明。[上下文文档](https://docs.langchain.com/oss/python/deepagents/context-engineering)、[记忆文档](https://docs.langchain.com/oss/python/deepagents/memory)

ACE 的 v1 方法采用有 ID 的结构化条目及局部更新，避免不断整段改写上下文造成细节流失；本次 v3 摘要页仍将上下文表述为整理、改进的过程指导。仅借鉴条目化和局部修订原则，没有复现其多角色学习流程，也不迁移其报告增益。[v1 方法](https://arxiv.org/html/2510.04618v1)、[v3 摘要页](https://arxiv.org/abs/2510.04618)

设计推断是：在上述成熟基础上，把修复阶段、竞争根因、恢复条件、保留行为和原版本失败共同用于每次决策上下文的构建，可以形成可消融的领域升级。具体分层/编译只在设计第 7.1 节维护。它仍可能只是较好的任务提示；必须观察实际读取与后续补丁、同预算效果及关闭阶段组织/失败记忆的比较，才能支撑论文主张。

## 复用层级必须说清楚

- 使用原始代码和原始配置，且实际核验行为：才能称为原方法复现。
- 借鉴论文思路，自行适配比赛输入：称为参考方法适配，不拿它的较弱实现代替原论文水平。
- 添加自己的证据检查或策略：用同一底座的成对实验隔离增量；论文再补最接近原方法的比较。
- 公开论文数字只能作背景。模型、数据、定位权限、预算或版本不一致的数字不放进本项目主结果表。

## 代码查阅结论

mini-swe-agent：已查阅 [Agent 主循环](https://raw.githubusercontent.com/SWE-agent/mini-swe-agent/main/src/minisweagent/agents/default.py)、[Docker 接口](https://raw.githubusercontent.com/SWE-agent/mini-swe-agent/main/src/minisweagent/environments/docker.py)、[依赖](https://raw.githubusercontent.com/SWE-agent/mini-swe-agent/main/pyproject.toml) 与 [MIT 许可证](https://raw.githubusercontent.com/SWE-agent/mini-swe-agent/main/LICENSE.md)。9 月 12 日所读代码的费用和墙钟限制在循环边界检查，单次调用可能越界；正式比赛需另设调用/进程级限制并记录越界，而不能声称配置阈值就是硬上限。读取的是 main 页面，未固定 SHA，未验证安装。

SHERLOC：论文提供 [官方 recipe](https://github.com/NVIDIA-NeMo/Skills/tree/main/recipes/sherloc)。已确认目录入口，未完成完整依赖与许可审核；整体采用前需要完成依赖、许可及接入成本评估。

ARISE：[仓库入口](https://github.com/FARD-Lab/ARISE) 已检查，个别 raw 文件读取未成功，尚不能承诺可以直接运行或复制代码。若采用，先核验许可证、固定版本及 Python 支持范围。

## 检索与不确定性记录

检索使用公开关键词，覆盖软件修复 Agent、运行时调试、仓库级定位、诊断质量迁移、测试选择、Skills、训练环境和日志驱动基准。没有向搜索服务提交私仓内容或未公开源码。

重点精读了 SHERLOC、ARISE、TraceRepair、DebugRepair 和 CI-Repair-Bench 的方法/评测相关段落；其他工作以原始摘要、官方项目或元数据筛查为主。未做逐篇全文复现、完整引用链追溯或特定投稿会场查新。边界不妨碍先做可关闭的增量模块，但正式论文主张仍需最后核对。
