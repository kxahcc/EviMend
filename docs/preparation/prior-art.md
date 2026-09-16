# 相关研究与复用决策

查阅日期：2026-09-12。采用“在成熟方法上升级”的定位。以下是面向技术决策的检索，不是系统综述或完整查新证明；录用状态若来自 arXiv Comments，明确视为作者页面声明。

## 优先使用和比较的工作

| 工作 / 来源状态 | 已覆盖的内容 | 本项目如何使用 |
|---|---|---|
| [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)，开源系统；MIT | 简单修复循环、模型和执行环境分离 | 工程底座第一候选；需要固定提交并进行实际验收 |
| [SWE-agent](https://arxiv.org/abs/2405.15793)，2024 方法；[官方代码](https://github.com/SWE-agent/SWE-agent) 标注 NeurIPS 2024 | 面向 Agent 的仓库操作与执行接口 | 专用工具需求较强时的备选；现有工具设计是借鉴基础 |
| [Agentless](https://arxiv.org/abs/2407.01489)，2024 方法 | 分阶段定位、修复、补丁验证 | 简单且有竞争力的流程对照；不以复杂程度判优 |
| [SHERLOC](https://arxiv.org/html/2606.24820v2)，2026 方法；作者页面标注 EMNLP 2026 Main | 结构化诊断交接、定位与修复效果/成本、质量过滤 | 最接近的诊断增强参考；区分可部署运行和事后分析 |
| [ARISE](https://arxiv.org/html/2605.03117v2)，2026 预印本与工具 | 仓库图、语句级数据流切片、跨框架工具接入 | 有数据流瓶颈时才引入，先用轻量检索；避免提前搭大图系统 |
| [AgentFL](https://arxiv.org/abs/2403.16362)，2024/2025 版本，未在本轮核实正式发表条目 | 项目级定位与测试行为跟踪 | 防止把“仓库级定位”本身描述成新问题 |
| [TraceRepair](https://arxiv.org/html/2604.02647v2)，作者页面标注 ASE 2026 | 运行探针、不同修复策略及基于证据的交叉检查 | 借鉴探针与证据；正式比较需对齐定位条件 |
| [DebugRepair](https://arxiv.org/html/2604.19305v1)，2026 预印本 | 定向运行状态收集和迭代修复 | 动态诊断近邻；不直接比较不同已知位置条件下的成功数 |
| [执行 Trace 的 APR 研究](https://aclanthology.org/2025.knowledgenlp-1.17/)，2025 workshop 方法/实证 | 直接增加 Trace 的收益不稳定，复杂度影响有效性 | 支持优先筛选和压缩证据，而不是无限增加上下文 |
| [SkillRevise](https://arxiv.org/abs/2606.01139)，作者页面标注 Findings of EMNLP 2026 | 用执行反馈改进技能 | Skills 的验证与更新可借鉴，第一轮先静态冻结 |
| [Socratic-SWE](https://arxiv.org/abs/2606.07412)，2026 预印本，页面标注 under review | 轨迹提炼技能，再引导训练任务与模型学习 | 中后期参考；不作为首轮必要依赖 |
| [SWE-Gym](https://arxiv.org/abs/2412.21139)，作者页面标注 ICML 2025；[SWE-smith](https://arxiv.org/abs/2504.21798)，2025 预印本 | 可执行训练环境、轨迹和任务生成 | 真正启动训练前再评估；训练并非无环境依赖的捷径 |

## 两个会改变实施决策的细节

SHERLOC 的质量过滤分析部分使用了正确补丁辅助评判；其无补丁验证器被评估的是与回顾性质量标签的一致性，论文注明尚未评估该选择器驱动的下游修复。因而“低额外预算、无正确补丁、真实执行全部分支”的增量实验具有明确比较对象。这是候选升级切口，不是已经证明的独占创新。[论文限制与验证分析](https://arxiv.org/html/2606.24820v2)

ARISE 明确报告了额外 Token 消耗，图构建时间也随仓库规模变化。由此作出的工程决策是：先验证数据流信息是否能挽回失败，再决定是否承担图构建成本；一次一仓的比赛场景还应计入首次建图成本。[成本分析](https://arxiv.org/html/2605.03117v2)

诊断测试选择和信息增益也不是新概念。2010 年 QSIC 工作已研究面向定位的测试优先级，2011 年存在相关期刊扩展。因此不把“按信息价值选择检查”作为独立首创；关注面向当前日志—代码—修复链路的具体实现与新增证据。[QSIC 2010 元数据](https://dblp.org/rec/conf/qsic/Gonzalez-SanchezPGG10)、[作者机构的期刊记录](https://research.tudelft.nl/en/publications/prioritizing-tests-for-software-fault-localization-2)

## 复用层级必须说清楚

- 使用原始代码和原始配置，且实际核验行为：才能称为原方法复现。
- 借鉴论文思路，自行适配比赛输入：称为参考方法适配，不拿它的较弱实现代替原论文水平。
- 添加自己的证据检查或策略：用同一底座的成对实验隔离增量；论文再补最接近原方法的比较。
- 公开论文数字只能作背景。模型、数据、定位权限、预算或版本不一致的数字不放进本项目主结果表。

## 代码查阅结论

mini-swe-agent：已查阅 [Agent 主循环](https://raw.githubusercontent.com/SWE-agent/mini-swe-agent/main/src/minisweagent/agents/default.py)、[Docker 接口](https://raw.githubusercontent.com/SWE-agent/mini-swe-agent/main/src/minisweagent/environments/docker.py)、[依赖](https://raw.githubusercontent.com/SWE-agent/mini-swe-agent/main/pyproject.toml) 与 [MIT 许可证](https://raw.githubusercontent.com/SWE-agent/mini-swe-agent/main/LICENSE.md)。当前费用和墙钟限制在循环边界检查，单次调用可能越界；正式比赛需另设调用/进程级限制并记录越界，而不能声称配置阈值就是硬上限。读取的是 main 页面，未固定 SHA，未验证安装。

SHERLOC：论文提供 [官方 recipe](https://github.com/NVIDIA-NeMo/Skills/tree/main/recipes/sherloc)。已确认目录入口，未完成完整依赖与许可审核；不建议为第一轮试验搬入整个大型代码库。

ARISE：[仓库入口](https://github.com/FARD-Lab/ARISE) 已检查，个别 raw 文件读取未成功，尚不能承诺可以直接运行或复制代码。若采用，先核验许可证、固定版本及 Python 支持范围。

## 检索与不确定性记录

检索使用公开关键词，覆盖软件修复 Agent、运行时调试、仓库级定位、诊断质量迁移、测试选择、Skills、训练环境和日志驱动基准。没有向搜索服务提交私仓内容或未公开源码。

重点精读了 SHERLOC、ARISE、TraceRepair、DebugRepair 和 CI-Repair-Bench 的方法/评测相关段落；其他工作以原始摘要、官方项目或元数据筛查为主。未做逐篇全文复现、完整引用链追溯或特定投稿会场查新。边界不妨碍先做可关闭的增量模块，但正式论文主张仍需最后核对。
