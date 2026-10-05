# 项目蓝图与开发导览

状态：当前开发入口。最后维护：2026-10-05。
职责：先用通俗语言说明要做什么、如何做和重点在哪，再提供文件导航。这里是设计的概览；具体技术决策以开发设计为准，实际进度以 STATUS 为准。

## 1. 我们最终要做出什么

**做一个面向 HarmonyOS 应用的故障定位与自修复 Agent：给它故障日志、应用源码和允许的测试入口，它自动找原因、修改代码、运行检查；失败后根据反馈继续修，最后交出补丁和验证报告。**

初版产品形态是命令行程序，可以处理单个故障，也可以批量处理任务清单。人负责准备任务、运行条件和可信的业务验收要求；系统负责每个任务内的分析、改代码和有界重试。首先把这条链路做好，展示界面以后视交付需要添加。

用户最终拿到三类结果：

- **定位结果**：哪个文件、哪几行造成问题，以及日志和代码依据。
- **修复产物**：实际可应用的代码补丁，说明改变了什么。
- **验证记录**：原故障是否复现、补丁后是否消失、功能/回归是否通过、尝试了几轮、用了多少资源。无法完成时明确指出卡在哪里。

这些是目标产品能力，不是现有原型已具备的功能。最近进度只在 [STATUS](docs/STATUS.md) 维护。

## 2. 用一个例子理解它怎样工作

例如：某页面加载数据时崩溃，日志指向一次 `Uint16Array` 构造。下面是基于已审查材料的设计推演，不是已验证修复。

1. **读故障**：找到报错位置，读取附近代码及相关调用，识别“缓冲区容量不够”。
2. **找原因**：发现前面按 N 个字节分配容量，后面却想放 N 个 16 位元素，需要 2N 个字节；报错发生在使用处，问题来自容量与使用方式不一致。
3. **检查业务含义**：继续读生成和校验逻辑，确认这段数据应该如何编码、谁还会使用它。
4. **提出补丁**：选择与这些用途一致的容量或构造方式，输出实际 diff；不能只靠缩短数据让异常消失。
5. **验证并调整**：先检查局部边界，再构建应用、重放目标操作并检查业务结果。如果不崩溃了但校验结果错误，反馈给下一轮修复。

我们想让系统学会的工作方式就是：**从“哪里报错”追到“哪个必要条件被破坏”，再修复原因并验证原有功能。**

## 3. 整个系统由什么组成

```mermaid
flowchart LR
    A[故障日志 + 应用源码] --> B[整理证据]
    B --> C[模型找原因]
    K[四类诊断规则] --> C
    C --> D[模型生成补丁]
    D --> E[本机受管构建 + 设备应用验证]
    E --> F{必要检查通过?}
    F -->|是| G[根因位置 + 补丁 + 验证报告]
    F -->|否| H[控制器分类失败并决定下一步]
    H --> C
    H -->|缺环境或预算耗尽| I[已有产物 + 未解决原因]
```

**大模型负责理解与修改，程序负责执行与判断流程是否完成。** 模型可以提出“应该修好了”，但只有实际验证记录满足协议，控制器才接受补丁。Agent、构建、设备回放与评测都规划在用户本机受管运行；基础模型拟由本机 Agent 调用云端大模型 API，不规划本机模型权重部署。

共同验收协议在看候选结果之前按故障变体和源码版本固定：原版必须在目标操作触发指定故障，候选须完成构建、目标、业务和回归检查。分析源码与最终运行工程分别绑定，缺检查或只有合成结果时只报告未验证。具体字段和拒绝条件在 [模块规格](docs/design-contracts.md#13-补丁验证和尝试)；当前代码只有隔离合成接受门，尚无应用结果。

## 4. 工作重点与自己的贡献在哪里

| 工作点 | 我们具体要设计/实现什么 | 在项目中的作用 |
|---|---|---|
| 完整修复闭环 | 读代码、调用模型、应用补丁、运行测试、记录失败并重试 | 参赛底座，首先保证端到端能工作 |
| 修复义务引导的根因分析 | 关联根因假设、必须恢复的条件和必须保留的业务行为，让同一记录贯穿定位、补丁与验证 | 拟议核心贡献：提高定位与修复选择质量 |
| 按决策需要检查与修订 | 简单故障直接修；不同解释会导向不同修法时才追加区分检查，失败反例更新对应义务 | 拟议核心贡献：减少错误方向和无效重试 |
| 上下文、经验与 Skill 演进 | 围绕当前假设与义务选择上下文；旧经验先核对适用条件，未知时提供查证步骤；验证后再晋升或退回 Skill | 拟议方法载体：减少证据遗漏与错误迁移，逐项比较后决定是否启用 |
| 业务验证与评测 | 检查修复是否保留原功能，公平比较基础系统和增强系统 | 证明效果，避免只消除崩溃却破坏功能 |

例如，两种修法都能避免越界，但只有一种保持原校验结果。Agent 应先查清这个差别，再决定修改；若已有代码证据足够，就不额外消耗一次运行。明确需要检查什么、何时检查，以及检查失败改变哪项决策，是我们要落实的方法细节。

条件检查还要确保输入用对了：例如容量和偏移都以字节计量，却不能互换。拟议实现先把参数绑定到精确代码操作，再读取有限源码事实或取得运行采样凭据，最后保留来源和未知项交给修复决策。源码里出现一个常量不代表实际使用时就是该值；暂时无法取值时仍可走通用修复路径。流程和实现边界见 [开发设计第 3.2.1 节](docs/development/system-design-draft.md#321-怎样取得可信的条件输入)。

**我们的重点是成熟 runtime 上的领域 harness。** Skills 属于方法设计和交付的一部分：把经验变成有适用前提、查证分支、保留行为及退出条件的过程，例如“容量与元素数怎么检查”。贡献通过可迁移程序、选择/更新机制和效果证明，文件数量不作为成绩。基础模型继续规划通过云端 API 接入通用大模型，不规划参数微调。

设计把 context、memory 和 Skills 连接起来：每一步先突出当前假设、业务义务与有效反例；检索到旧经验后，用当前证据判断其前提。适用时提供步骤，矛盾时指出不适用，未知时提供区分问题。任务内反馈继续修代码；跨任务反馈先形成候选经验，经独立开发案例和边界反例验证后才更新冻结 Skill。工作上下文与普通经验检索已接入实际 SDK：主机固定批准知识，Agent 获取完整指导，查询和实际工具结果随调用账本、报告及私有导出保存。普通检索是共同基线，相关性排序不判断能否照搬修法。三态工具以每次实际请求为依据核对当前焦点（实际请求中的诊断、所选假设和当前候选）及逐前提引用，支持时提供完整过程，矛盾时提示暂停照搬，未知时保留查证问题；判断仍为模型推断。启停两组共用相同工具、知识和预算，完整指导及调用来源随报告和私有导出保存。知识、三态和静态 Skill 已有完整 SDK 会话、报告、对照及私有导出入口，固定/模拟脚本通过显式主机批准接入同一受管 CLI。启动消费已经批准的冻结库，恢复使用 run 原件和原批准，不要求原库存在；反馈提炼与候选演进继续按既定顺序实现。详细流程见 [设计第 7 节](docs/development/system-design-draft.md#7-skills-的具体位置)，字段和失败行为见 [规格第 3.1 节](docs/design-contracts.md#31-工作上下文经验与-skill-的实施契约)，实际完成与下一包见 STATUS。

论文希望回答的具体问题是：**相同模型、知识、工具和预算下，围绕当前修复义务组织证据并迁移经验，是否能减少错误尝试，交出更多保留功能的补丁？** 以静态 Skill、普通案例检索和可关闭的机制比较解释收益；目前没有效果证据。

我们的竞争力目标是：**同模型、同工具和总预算下，比强通用代码 Agent 少做错误尝试，交出更多保留功能的正确补丁。** 先做强基础版，再逐项加入增强；简单问题直接修，存在影响修法的歧义才查证。增强造成的失败和额外成本也要计入，不能只展示被救回的案例。

审阅具体方案时，先看 [开发设计第 7 节](docs/development/system-design-draft.md#7-skills-的具体位置) 的上下文/记忆/演进流程，再看 [第 8 节](docs/development/system-design-draft.md#8-竞争力设计与成立条件) 的任务内机制、完整案例和成立条件。比较公平性与接受条件见 [评测口径](docs/preparation/metrics-and-gates.md)。这些是可检验的设计假设，尚非效果结论。

## 5. 开发框架和资源

**Python 3.13 + Deep Agents SDK（底层 LangGraph）+ Pydantic 2 + langchain-core + SQLite** 是首版 Agent 技术底座。模型边界目前用既有 HTTPX 的模拟传输验证，真实云端提供方确定后再核对其适配方式；应用验证规划由本机受管子进程调用匹配的 **HarmonyOS 工具链与设备/模拟器**。详细选择理由、恢复边界和依赖接入门槛统一见 [开发设计](docs/development/system-design-draft.md)。

| 部分 | 用什么 | 为什么 |
|---|---|---|
| Agent 流程 | Deep Agents SDK | 复用文件工具、上下文管理与工具循环；我们实现诊断策略及验证约束 |
| 输入输出 | Pydantic 2 | 检查模型动作、证据、候选与验证结果的结构 |
| 模型 | langchain-core 模型入口、持久调用账本与显式请求适配器 | 已能核对模拟请求的消息、工具、参数与响应；真实云端提供方、型号及传输仍待接入，不在本机部署权重 |
| 状态与轨迹 | SQLite 检查点、JSONL 事件导出、文件产物 | 本地即可运行和检查，不先部署数据库服务 |
| 环境管理 | uv + pyproject.toml + uv.lock | 项目内隔离依赖并锁定版本；固定响应 SDK 切片已做合成验证 |
| 代码工具 | Python、ripgrep、Git CLI | 按需读源码、生成/检查补丁、维护候选目录 |
| 应用执行 | 本机受管作业进程、SQLite 意图/结果与 HarmonyOS 命令行工具 | 与 Agent 隔离环境、绑定候选版本并可停止/恢复；本地真实适配器待实现 |
| 应用验证 | 本机项目匹配的 DevEco/SDK、Hvigor、ohpm、hdc，按需 Hypium | 先核对本机容量和设备/模拟器可用性，再受管构建、回放及业务验收 |
| 开发检查 | pytest、Ruff、Pyright | 合成测试、接口检查、格式和类型检查 |

Deep Agents 提供通用 Agent 能力，LangGraph 承担底层运行；研究增量围绕修复义务组织决策，工作上下文和有条件的经验/Skill 迁移服务于同一主线。关闭 SDK 自动摘要和消息/工具结果卸载，采用明确的上下文上限，并提供可选的主机压缩：保留完整历史，只缩短模型发送视图，摘要独立记账和恢复，不能代替检查事实。具体协议见模块规格第 1.2.2 节。首版不叠加多 Agent 框架、云端追踪平台或消息队列；mini-swe-agent 保留为外部工程参照。我们仍须实现领域工具、候选登记、幂等作业和业务验证，框架不会自动解决这些问题。

这一底座已完成依赖锁、首批领域对象、固定响应及模拟模型协议的离线修复循环、模拟 HTTP 作业协议、可关闭的领域策略分支和调用账本。最终报告把初次定位、最新定位、候选补丁、检查与未知用量分开，可从已保存事实核验摘要。SDK 工具调用先由主机授权，恢复时回放已完成结果，未保存的执行结果保持未知；真实云端传输、可构建工程与本机应用验证尚待实现，进度看 STATUS。

### 本机资源怎样集中管理

本机实现采用仓库内 .dfx-local 作为项目环境、缓存、临时副本和运行记录的集中位置；离线证据可通过受管 Export 保留到 runs/exports。日常只启动一个受管任务。真实应用阶段先盘点设备、SDK/构建缓存的占用，再为其建立同样受管的安装、运行和清理入口；当前方案没有本机模型权重占用。停止运行、清缓存、删除环境是不同操作，均只针对本项目归属的资源。

管理入口 scripts/local.ps1 提供 Prepare、Status、Clean、Stop、Init、固定合成/测试/质量/离线 Agent/只读聚合的受限 Run，以及私有证据 Export。任务先建立 Windows Job 的 CPU、提交内存和进程数限制再启动；输出经有上限的管道收集。项目 Python、uv、虚拟环境与缓存集中在 .dfx-local，managed 与 exports 共同受磁盘预算约束。真实模型/本机应用任务仍未接入；[本机配置](configs/local-runtime.json) 的位置标记为 local，安装/实验开关仍关闭。完整目录、限制边界和清理协议见 [开发设计第 9 节](docs/development/system-design-draft.md#9-本机环境资源限制与集中清理)。

每次新离线运行保存执行源码指纹、依赖锁和实际 Python/依赖版本。恢复先核对兼容性，报告和聚合只读取当时保存的事实；旧记录的缺失版本保持未知。Git 的初始观察与续跑身份分开，日常改文档不会改变执行源码的兼容身份。完整约束见 [模块规格](docs/design-contracts.md)。

Export 从显式评测清单保留任务源码、全部候选、账本和失败记录：先预览内容与计划，再按 PlanId 应用，最后只读 Verify 从包内重建同一摘要。包含原始消息和私密源码，不能直接公开；导出不删除源，也不自动放行清理。终态 work、任务快照及未发布暂存仍须在具体清理请求中明确丢弃。实际验收看 STATUS；真实应用汇总、工具链/设备来源和公开脱敏另待后续接入，使用命令见 [原型说明](src/dfx_agent/README.md)。

## 6. 怎样一步步把它做出来

先得到**设计闭合、可自检的流程骨架**，再得到**能修完一个真实案例的基础系统**，然后加入**可单独比较的约束和反馈增强**，最后扩大覆盖并整理参赛与论文证据。详细阶段和验收门槛见 [实施计划](docs/project-work-plan.md)。

本机设备/模拟器尚未就绪时可把算法步骤、模型/工具交互、失败处理和离线控制逻辑设计具体。设备用于证明原故障能复现、补丁确实有效，以及测量运行开销。我们不会把所有设计决定拖到设备到位之后，也不会在没有实际结果时声称方案已经有效。

后续实施围绕完整任务路径逐步打通。换模型接手时，先读 STATUS 指定的下一包，再按 [实施计划的工作包](docs/project-work-plan.md#可直接接手的实现工作包) 查看修改范围、前置依赖和失败用例；每包通过检查再提交。资源保护和停止/清理优先于依赖安装，基础 Agent 优先于效果增强。

## 7. 文档怎么用

**理解项目先读到这里即可。** 想看做到哪一步，打开 [STATUS](docs/STATUS.md)；想看具体算法，打开 [开发设计](docs/development/system-design-draft.md)；准备实现模块时再查 [模块规格](docs/design-contracts.md)。下方是目录索引，不需要按文件顺序通读。

## 文档登记表

表中的“当前”表示唯一在用的文档，不代表其设计已经实现。证据文档的采集日期反映观察时间；提交模板的填写日期不承担开发版本管理。所有受维护 Markdown 均在此登记。

<!-- document-registry:start -->
| 路径 | 类型 | 唯一职责 |
|---|---|---|
| `DEVELOPMENT.md` | 当前入口 | 项目蓝图、阅读顺序、结构与文档登记 |
| `AGENTS.md` | 当前规则 | 助手工作流程与维护约定 |
| `docs/STATUS.md` | 当前状态 | 进度、未决条件、下一步、实测状态 |
| `docs/project-work-plan.md` | 当前计划 | 阶段顺序、依赖、完成门槛、设计缺口 |
| `docs/development/system-design-draft.md` | 当前设计 | 架构、算法与后端抽象 |
| `docs/design-contracts.md` | 当前规格 | 字段不变量、状态转换、规则卡 |
| `docs/preparation/metrics-and-gates.md` | 当前评测 | 指标、对照与验收口径 |
| `docs/preparation/data-b-assessment.md` | 当前证据 | 数据事实、异常及来源；正文分别标明采集日期 |
| `docs/preparation/prior-art.md` | 当前证据 | 文献查阅事实、可借鉴内容及限制 |
| `src/dfx_agent/README.md` | 当前说明 | 离线原型命令、输入格式和边界 |
| `scripts/preparation/README.md` | 当前说明 | 数据盘点工具用法 |
| `README.md` | 提交模板 | 竞赛总入口 |
| `docs/technical-design.md` | 提交模板 | 正式技术设计 |
| `docs/test-report.md` | 提交模板 | 正式测试报告 |
<!-- document-registry:end -->

## 目录与文件

| 位置 | 用途 |
|---|---|
| `docs/` | 上表中的阶段、设计、规格、评测与证据文档 |
| `src/dfx_agent/` | Python 离线证据原型、候选工作区、固定响应修复闭环、模拟 HTTP 协议及受控策略；模块职责见下表 |
| `scripts/dfx.py` | 加载 src 包，提供 doctor / prepare / analyze 和受管 repair / resume / aggregate / export / verify-export 入口 |
| `scripts/local.ps1` | 本机 Prepare、Status、Clean、Stop、Init、受限合成/测试/质量/离线 Agent/聚合及私有 Export/Verify 入口；真实模型/应用任务尚未接入 |
| `scripts/local_job.cs`、`scripts/local_synthetic.ps1` | Windows Job/输出管道封装与固定合成工作负载；不执行模型或应用任务 |
| `scripts/local_bootstrap.ps1`、`scripts/local_test.ps1`、`scripts/local_quality.ps1` | Job 内的固定依赖安装、pytest 和文档/Ruff/Pyright 检查，不接受任意 shell 命令 |
| `scripts/local_agent.ps1` | Job 内固定响应或模拟模型协议的离线 repair / resume 工作进程，输入由管理入口限定 |
| `scripts/local_metrics.ps1` | Job 内只读核验显式任务清单，生成新的离线汇总与终态；不调用模型或检查 |
| `scripts/local_export.ps1` | Job 内固定私有 Export 预览、Apply 和只读 Verify 工作进程，核对管理租约与项目环境 |
| `scripts/test_local_runtime.ps1`、`scripts/test_local_process.ps1`、`scripts/test_local_bootstrap.ps1` | W01 路径清理、W02 进程保护和 W03 重复 Init 的合成验收 |
| `scripts/check_docs.py` | 文档登记、链接、维护元数据及 archive 跟踪检查 |
| `scripts/preparation/inspect_dfx_data.py` | 只读盘点本地 data_B；不执行数据包脚本 |
| `tests/test_offline_evidence.py`、`tests/test_case_identity.py`、`tests/test_sdk_compat.py` | 原型合成输入、显式案例身份与固定响应 SDK 接入自检；最近结果由 STATUS 记录 |
| `tests/test_contracts.py`、`tests/test_workspace.py` | 领域对象的拒绝条件与候选隔离、登记、回退的合成反例 |
| `tests/test_conditions.py`、`tests/test_condition_store.py`、`tests/test_condition_handoff.py` | 有限条件计算、不可改写结果及固定响应 Agent 交接，含单位/范围/来源、版本、恢复和开关反例；不是应用评测 |
| `tests/test_value_sources.py` | 独立主机合成操作绑定与采值凭据反例：原字节锚点、角色/事件、不可改写登记和回读；不证明真实语法解析或运行采样 |
| `tests/test_knowledge_contracts.py`、`tests/test_knowledge_store.py` | 独立经验/快照的完整内容、主机权限交集、原字节保存、总容量与闭合回读反例；不证明经验适用性、自动学习或修复收益 |
| `tests/test_retrieval_contracts.py`、`tests/test_knowledge_retrieval.py` | 冻结指导的确定性标签/文本排序、未知标签保留、分页、记录重算、SQLite 原件/索引错配及容量反例；不是语义适用性或真实效果评测 |
| `tests/test_applicability_contracts.py`、`tests/test_applicability.py` | 当前任务/批准知识绑定、逐前提三态建议、原版/候选引用范围、冻结回读和容量反例；不证明模型判断或经验迁移收益 |
| `tests/test_knowledge_run.py`、`tests/test_knowledge_sdk.py`、`tests/test_knowledge_reporting.py` | 批准知识复制、实际 SDK/模拟请求、预算与只读恢复、查询/回执全文审计、同知识矩阵及迁移导出；不证明语义适用或修复收益 |
| `tests/test_solver_visibility.py`、`tests/test_applicability_handoff.py`、`tests/test_applicability_sdk.py`、`tests/test_applicability_provider.py`、`tests/test_applicability_reporting.py` | 实际发送依据、三态完整指导、未知回退、兄弟结果未见、共同预算、恢复与原件审计、同工具对照及迁移导出 |
| `tests/test_static_skills.py`、`tests/test_static_skill_handoff.py`、`tests/test_static_skill_sdk.py`、`tests/test_static_skill_resume_cache.py`、`tests/test_static_skill_reporting.py` | 静态编译、原生目录、完整读取、共同预算、固定/模拟 SDK 失败后修订、恢复前漂移拒绝、实际可见性、启停对照及私有导出；合成调用不证明修复收益或学习效果 |
| `tests/test_working_context.py` | 显式焦点、来源复核、义务/证据闭包、历史反例范围、冻结回读和必要包超限的合成反例；不证明实际模型已使用或效果 |
| `tests/test_working_context_scale.py` | 同一千文件合成任务的 off/on 分析器测量及原默认预算复验；重复/不同内容分开，部分 I/O 代理与总调用耗时分开，不是模型或应用效果评测 |
| `src/dfx_agent/working_context_plan.py`、`tests/test_working_context_plan.py`、`tests/test_working_context_handoff.py` | SDK 可见工具结果到当前焦点、必要上下文和实际发送；冻结来源、完整组、后续检查进展与崩溃/未知恢复的合成验收 |
| `src/dfx_agent/working_context_materials.py`、`tests/test_working_context_materials.py` | 同 run 原字节内容寻址、不可改写引用、链接/读中变化/崩溃残留及容量验收；不跨 run 接管材料 |
| `src/dfx_agent/working_context_projection.py`、`tests/test_working_context_projection.py` | 从完整审计 packet 生成有界模型视图；仅替换来源清单，保留全部领域内容及未知/权限范围 |
| `tests/test_working_context_reporting.py`、`tests/test_working_context_metrics.py` | 新代发送计划回读、旧代不回填、显式上下文因素、公平门及私有导出合成验收 |
| `tests/test_agent_loop.py`、`tests/test_remote_contract.py`、`tests/test_policies.py`、`tests/test_action_acceptance.py`、`tests/test_model_calls.py`、`tests/test_tool_authorization.py`、`tests/test_causal_diagnosis.py`、`tests/test_decisions.py` | 固定响应闭环、模拟 HTTP、领域策略、动作/应用接受、模型与工具恢复、因果诊断及修复决策交接的合成验收 |
| `tests/test_managed_knowledge_workflow.py` | 固定/模拟 CLI 全流程及真实 Windows Job 验收的合成 fixture，核完整 Skill 可见性与同资源启停矩阵 |
| `tests/test_model_provider.py`、`tests/test_provider_loop.py`、`tests/test_reporting.py`、`tests/test_offline_script.py`、`tests/test_offline_cli.py` | 模拟模型请求/响应、SDK 交接、最终摘要核验和受管脚本/命令入口；不调用真实模型 |
| `tests/test_context_boundary.py` | 实际 SDK 的有界上下文、隐式摘要/文件卸载关闭、调用前拒绝及原版保护的合成验收 |
| `tests/test_context_compaction.py`、`tests/test_compaction_loop.py` | 主机压缩的完整工具组、冻结来源、独立费用、格式失败、未知结果、恢复及实际 SDK 交接的合成验收 |
| `tests/test_readonly_evidence.py`、`tests/test_metrics.py`、`tests/test_metrics_cli.py` | 不可变账本、完整分母/未知项/重复运行、配置漂移及受管聚合入口的合成验收 |
| `tests/test_exporting.py`、`tests/test_export_cli.py`、`tests/test_cleanup.py` | 完整私有证据包、计划/输入保存、包内重聚合、篡改/容量、受管导出入口及证据清理拒绝的合成验收 |
| `tests/test_provenance.py`、`tests/test_provenance_loop.py`、`tests/test_provenance_reporting.py`、`tests/test_provenance_metrics.py` | 运行来源自洽、源码/依赖漂移拒绝、SDK/CLI 提前门控、旧记录只读兼容及混合汇总的合成验收 |
| `resources/preparation/data-b-inventory.json` | 数据盘点结果；不是根因标签或修复成绩 |
| `resources/preparation/sources.json` | 研究来源登记；不是当前实施配置 |
| `sessions/` | 按工作批次保存的历史摘要，允许保留当时的旧路径与决定 |
| `skill/` | 项目 Skill 的模板位置；实际实现状态见 STATUS |
| `data_B/` | 本地竞赛数据；Git 忽略 |
| `configs/local-runtime.json` | 本机资源/清理策略；合成 Run、Init、测试、质量、离线 Agent、聚合和 Export 的受管限制与共同磁盘预算，字段语义见设计与契约 |
| `configs/bootstrap.json` | 固定 uv 下载身份、大小上限和项目 Python 版本 |
| `.dfx-local/` | 本机环境、缓存、任务/工作副本、导出暂存与运行状态根；Git 忽略，所有当前运行入口受管 |
| `runs/` | 历史原型产物、管理工具接入前的临时材料及本机私有 exports；Git 忽略，普通清理保留导出包 |
| `archive/` | 失效文档及配套材料；Git 忽略，不作为当前入口或运行依赖 |
| `pyproject.toml`、`uv.lock`、`.python-version` | Python 3.13 基线、SDK 与质量工具的已解析依赖锁 |
| `.gitignore` | 排除本地数据、归档、运行产物、缓存、虚拟环境和凭据文件 |
| `.gitkeep` / 各目录 README.txt / template-session.json | 竞赛模板的占位及格式说明 |

## src 模块职责

当前命令行为以 [原型说明](src/dfx_agent/README.md) 和代码为准，项目整体能力以 STATUS 为准。

| 文件 | 职责与边界 |
|---|---|
| `task.py` | 显式案例目录契约、日志/ZIP 成员与场景脚本哈希核对、分析快照和身份摘要；不生成完整运行工程 |
| `logs.py` | 编码、错误字段与栈帧解析；不推理业务根因 |
| `sources.py` | 源码路径匹配、歧义和邻近片段；不是 ArkTS 数据流分析器 |
| `analysis.py` | 汇总栈证据；源码命中不能当作根因预测 |
| `contracts.py` | Pydantic 证据、因果诊断、修复义务、候选及动作对象；来源、版本、绑定与引用校验不证明因果正确 |
| `diagnostics.py` | 主机读取引用事实、接纳模型因果提案、核对失败候选版本、保存不可改写的诊断包；不自动推理或执行运行条件检查 |
| `decisions.py` | 将修复方向与已登记诊断、义务、实际合成失败及同范围条件结果绑定，保存可回读建议及候选方向引用；不执行检查或确认因果、业务正确性 |
| `conditions.py` | 对主机固定合成输入计算索引、字节/元素和使用时就绪条件，保存并重新核验版本/范围绑定结果，供 Agent 回读引用；不证明源码产生这些值 |
| `value_sources.py` | 独立核对冻结源码字节、主机 fixture 的参数角色及模拟操作事件，登记绑定、意图与合成凭据；未接入 SDK、真实解析/采样或新来源条件计算 |
| `knowledge_contracts.py` | 不可变经验、求解指导、来源权限与知识快照对象；明确前提、未知问题、保留行为、反例和有限历史范围，不授予应用资格 |
| `knowledge.py` | 捕获主机批准的原字节、按来源交集保存经验和冻结快照、受容量限制完整回读；为独立检索复用同操作闭包，不接 SDK 或自动学习 |
| `retrieval_contracts.py` | 完整查询、索引描述、排序命中、分页与不可变查询记录；规范匹配和容量/数值边界不证明当前任务适用性 |
| `knowledge_retrieval.py` | 显式建立快照索引、只读检索完整指导、保存及重算查询记录；核对索引全部行与原件，不读取最新库或历史审计答案 |
| `applicability_contracts.py` | 主机任务批准、绑定查询、冻结依据、逐前提判断与完整三态记录；语义地位固定为模型推断 |
| `applicability.py` | 独立绑定当前任务和知识，核验登记证据/候选范围，保存并从冻结原件复核建议；不确认模型判断的语义真值 |
| `knowledge_run.py` | 将批准快照和完整来源闭包固定到 run，认证会话知识配置和外部恢复批准；不自动接管整库或扩大范围 |
| `knowledge_handoff.py` | 普通经验搜索/回读 SDK 工具、查询容量预检及实际调用/回执全文重算；历史指导不升级为当前事实 |
| `solver_visibility.py` | 在实际模型请求发出前固定最终输入；从完整已发送结果与工作上下文取得可见依据，摘要不授予事实引用权限 |
| `applicability_handoff.py` | 三态准备、审查和回读 SDK 工具；绑定实际输入焦点，保留完整指导、未知问题和调用来源，支持同工具关闭对照 |
| `static_skills.py` | 从 run 批准人工知识生成冻结过程正文/清单，复用原生 SkillsMiddleware 并核验资源和缓存；开启注入目录，关闭保留相同正文和读取能力 |
| `static_skill_handoff.py` | 将完整 read_skill 结果与父模型/动作账本、实际后续输入和报告关联；分开记录读取与可见性，提供启停共同容量门；受管配置由 offline_script/CLI 消费，反馈演进另待接入 |
| `actions.py` | SDK 工具中间件、主机能力/引用/预算检查及 SQLite 动作结果账本；不提供真实应用执行 |
| `workspace.py` | 从已校验分析快照建立独立草稿、原子文本编辑、冻结完整 diff 与候选哈希，并校验恢复归属；不执行应用检查 |
| `models.py` | 固定响应及模拟请求模型经账本接入同一 Deep Agents 循环，实施调用/已知 Token/时间预算；真实云端传输待实现 |
| `context_compaction.py` | 主机选择闭合历史前缀、冻结原始来源和摘要、保存实际发送视图并回读核验；摘要始终是模型推断，不修改 Graph 历史或原版 |
| `working_context.py` | 独立从显式主机焦点和已登记记录构建只读上下文包，复核来源/版本和引用闭包；实际发送与历史材料由 working_context_plan/materials/projection 接入 |
| `model_provider.py` | 严格配置、实际消息/工具/参数序列化、模拟 Chat Completions 请求/响应与原始 usage；只允许 MockTransport，不接受凭据或真实端点 |
| `offline_script.py` | 校验固定响应及模拟协议 JSON 脚本、策略与限额，建立隔离响应流；期望答案只供合成执行器 |
| `reporting.py` | 从版本、诊断、模型/工具账本和检查重建最终摘要，核验补丁及可验证报告字段；不执行模型或应用 |
| `metrics.py` | 按显式 task/variant/repeat 清单只读核验停止记录，汇总初始定位、全部候选及完整成本；应用指标未知 |
| `evidence_io.py` | 不可变只读 SQLite 连接，拒绝缺文件、链接/硬链及未合并日志；不修复或新建账本 |
| `provenance.py` | 运行源码/依赖锁及实际 Python/依赖版本观察、自洽哈希和恢复兼容检查；初始 Git 不用于填补历史或认证二进制 |
| `exporting.py` | 显式矩阵的私有计划/证据包、完整字节清单及包内重聚合核验；保留原消息与全部尝试，不执行 Graph、不删源、不自动授权清理 |
| `model_calls.py` | 持久模型请求账本、响应回放、格式/usage 分类及崩溃窗口拒绝重发；已用于固定响应路径，尚未接入真实提供方 |
| `agent.py` | Deep Agents 离线循环，接入任务指引、跨文件检索、未确认诊断、候选门控、可关闭策略工具和停止报告 |
| `task_strategy.py` | 基础/增强共享的明确修复任务指引与主机任务上下文；增强按开关追加，不证明模型效果 |
| `source_search.py` | 有限字面检索，返回原版/冻结候选的版本、文件和行号；分页、片段和编码缺项明确，不分析真实数据流 |
| `policies.py` | 四类可迁移规则提示、义务/合成反例引用及按修法区分力选择查证的建议；不执行探针或证明应用效果 |
| `acceptance.py` | 候选无关的共同协议、原版/候选执行记录及保守接受门；仅合成契约，不产生应用证据 |
| `verification.py` | 冻结候选专属的离线检查意图、幂等键和合成执行器；无应用成功判定 |
| `backends.py` | 版本绑定的 HTTP 模拟请求/作业对象、客户端意图与查询/取消/产物校验；本机真实执行适配器尚未实现 |
| `remote_service.py` | SQLite 持久化的 FastAPI 模拟作业服务和显式测试工作者状态；保留作协议反例测试，不是计划中的运行服务 |
| `runtime.py` | 当前仅观察 PATH/模块/环境变量存在性；不启动 SDK/设备 |
| `cli.py` | 参数与命令分发；repair / resume 只允许受管固定响应或模拟协议演示，aggregate / export / verify-export 同样经受管入口 |
| `__init__.py` | Python 包入口 |

## 查看与检查

从仓库根目录运行：

```powershell
.\scripts\local.ps1 Status
.\scripts\local.ps1 Init
.\scripts\local.ps1 Run -Task test -TestPath tests/test_sdk_compat.py
.\scripts\local.ps1 Run -Task docs
.\scripts\local.ps1 Run -Task test -TestPath tests/test_cleanup.py
```

受管 pytest 可运行 `-Task test -TestPath tests/test_agent_loop.py` 检查闭环。受管离线演示的输入约定见 [原型说明](src/dfx_agent/README.md)；它只使用固定响应或进程内模拟传输和合成判据，不启动真实模型或应用。盘点工具用法见其 README，真实任务输入必须显式提供经核验的清单。

## Git 与归档

本地 Git 保存当前源码、模板、文档和历史工作摘要，尚未配置远程。`git status --short` 查看变更，`git log --oneline -5` 查看批次。当前版本只保留一套生效决策，旧版本通过 Git 查询。

失效材料移入 archive 并从 Git 索引移除；目录中的本地说明记录替代入口。新克隆不会含本地归档，当前文档/脚本不得依赖它。历史 session 是已发生行为的记录，旧日期和旧路径不视为当前决策；不为消除历史差异而篡改它们。

新增当前 Markdown 先确认独立职责，再登记；其余细则统一见 [AGENTS](AGENTS.md)。
