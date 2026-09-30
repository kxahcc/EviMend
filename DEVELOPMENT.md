# 项目蓝图与开发导览

状态：当前开发入口。最后维护：2026-09-30。
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
| 业务验证与评测 | 检查修复是否保留原功能，公平比较基础系统和增强系统 | 证明效果，避免只消除崩溃却破坏功能 |

例如，两种修法都能避免越界，但只有一种保持原校验结果。Agent 应先查清这个差别，再决定修改；若已有代码证据足够，就不额外消耗一次运行。明确需要检查什么、何时检查，以及检查失败改变哪项决策，是我们要落实的方法细节。

Skills 是上述知识和动作的组织方式，例如“容量与元素数怎么检查”，不单独把 Skill 文件的数量当贡献。当前不安排小模型微调；先用强基础模型和可归因的 Agent 机制取得效果证据，后续只有出现明确且可学习的瓶颈才另行评估训练工作。

论文希望回答的具体问题是：**相同模型和预算下，让修复过程围绕可检查的运行条件组织，是否比普通修复循环更准确、更可靠或更省成本？** 有收益再用消融解释收益来自哪里；目前没有成绩可以证明它成立。

我们的竞争力目标是：**同模型、同工具和总预算下，比强通用代码 Agent 少做错误尝试，交出更多保留功能的正确补丁。** 先做强基础版，再逐项加入增强；简单问题直接修，存在影响修法的歧义才查证。增强造成的失败和额外成本也要计入，不能只展示被救回的案例。

审阅具体方案时，重点看 [开发设计第 8 节](docs/development/system-design-draft.md#8-竞争力设计与成立条件)：它说明四项工作怎样执行、在哪些任务上可能有收益、如何失效，并用一个完整案例串联。比较公平性和收益接受条件见 [评测口径](docs/preparation/metrics-and-gates.md)。这些目前是可检验的设计假设，尚非效果结论。

## 5. 开发框架和资源

**Python 3.13 + Deep Agents SDK（底层 LangGraph）+ Pydantic 2 + LangChain 模型适配包 + SQLite** 是首版 Agent 技术底座。应用验证规划由本机受管子进程调用匹配的 **HarmonyOS 工具链与设备/模拟器**；现有 HTTPX/FastAPI 代码是模拟协议原型。详细选择理由、恢复边界和依赖接入门槛统一见 [开发设计](docs/development/system-design-draft.md)。

| 部分 | 用什么 | 为什么 |
|---|---|---|
| Agent 流程 | Deep Agents SDK | 复用文件工具、上下文管理与工具循环；我们实现诊断策略及验证约束 |
| 输入输出 | Pydantic 2 | 检查模型动作、证据、候选与验证结果的结构 |
| 模型 | 由 ModelClient 包装所选云端大模型 API 的提供方适配包 | 本机 Agent 发起并记账；具体提供方、型号和 API 兼容性待确定，不在本机部署权重 |
| 状态与轨迹 | SQLite 检查点、JSONL 事件导出、文件产物 | 本地即可运行和检查，不先部署数据库服务 |
| 环境管理 | uv + pyproject.toml + uv.lock | 项目内隔离依赖并锁定版本；固定响应 SDK 切片已做合成验证 |
| 代码工具 | Python、ripgrep、Git CLI | 按需读源码、生成/检查补丁、维护候选目录 |
| 应用执行 | 本机受管作业进程、SQLite 意图/结果与 HarmonyOS 命令行工具 | 与 Agent 隔离环境、绑定候选版本并可停止/恢复；本地真实适配器待实现 |
| 应用验证 | 本机项目匹配的 DevEco/SDK、Hvigor、ohpm、hdc，按需 Hypium | 先核对本机容量和设备/模拟器可用性，再受管构建、回放及业务验收 |
| 开发检查 | pytest、Ruff、Pyright | 合成测试、接口检查、格式和类型检查 |

Deep Agents 提供通用 Agent 能力，LangGraph 承担底层运行；研究增量仍在修复义务与检查选择。首版不叠加多 Agent 框架、云端追踪平台或消息队列；mini-swe-agent 保留为外部工程参照。我们仍须实现领域工具、候选登记、幂等作业和业务验证，框架不会自动解决这些问题。

这一底座已完成依赖锁、首批领域对象、固定响应离线修复循环、模拟 HTTP 作业协议、可关闭的领域策略分支及固定响应模型调用账本；真实模型入口、可构建工程与本机应用验证尚待实现，进度看 STATUS。

### 本机资源怎样集中管理

本机实现采用仓库内 .dfx-local 作为项目环境、缓存、临时副本和运行记录的集中位置；需要长期保留的证据计划导出到 runs/exports。日常只启动一个受管任务。真实应用阶段先盘点设备、SDK/构建缓存的占用，再为其建立同样受管的安装、运行和清理入口；当前方案没有本机模型权重占用。停止运行、清缓存、删除环境是不同操作，均只针对本项目归属的资源。

管理入口 scripts/local.ps1 已提供 Prepare、Status、Clean、Stop、Init，以及固定合成、测试/质量和固定响应离线 Agent 的受限 Run。任务先建立 Windows Job 的 CPU、提交内存和进程数限制再启动；输出经有上限的管道收集。项目 Python、uv、虚拟环境与缓存集中在 .dfx-local。真实模型/本机应用任务与 Export 尚未接入；[本机配置](configs/local-runtime.json) 的位置标记已改为 local，但安装/实验开关仍关闭。完整目录、限制边界和清理协议见 [开发设计第 9 节](docs/development/system-design-draft.md#9-本机环境资源限制与集中清理)。

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
| `scripts/dfx.py` | 加载 src 包，提供 doctor / prepare / analyze 和受管 repair / resume 入口 |
| `scripts/local.ps1` | 本机 Prepare、Status、Clean、Stop、Init 与受限合成/测试/质量/离线 Agent 任务入口；真实模型/应用任务尚未接入 |
| `scripts/local_job.cs`、`scripts/local_synthetic.ps1` | Windows Job/输出管道封装与固定合成工作负载；不执行模型或应用任务 |
| `scripts/local_bootstrap.ps1`、`scripts/local_test.ps1`、`scripts/local_quality.ps1` | Job 内的固定依赖安装、pytest 和文档/Ruff/Pyright 检查，不接受任意 shell 命令 |
| `scripts/local_agent.ps1` | Job 内固定响应离线 repair / resume 工作进程，输入由管理入口限定 |
| `scripts/test_local_runtime.ps1`、`scripts/test_local_process.ps1`、`scripts/test_local_bootstrap.ps1` | W01 路径清理、W02 进程保护和 W03 重复 Init 的合成验收 |
| `scripts/check_docs.py` | 文档登记、链接、维护元数据及 archive 跟踪检查 |
| `scripts/preparation/inspect_dfx_data.py` | 只读盘点本地 data_B；不执行数据包脚本 |
| `tests/test_offline_evidence.py`、`tests/test_case_identity.py`、`tests/test_sdk_compat.py` | 原型合成输入、显式案例身份与固定响应 SDK 接入自检；最近结果由 STATUS 记录 |
| `tests/test_contracts.py`、`tests/test_workspace.py` | 领域对象的拒绝条件与候选隔离、登记、回退的合成反例 |
| `tests/test_agent_loop.py`、`tests/test_remote_contract.py`、`tests/test_policies.py`、`tests/test_action_acceptance.py`、`tests/test_model_calls.py` | 固定响应闭环、模拟 HTTP、领域策略、动作/应用接受及模型调用恢复契约的合成验收 |
| `resources/preparation/data-b-inventory.json` | 数据盘点结果；不是根因标签或修复成绩 |
| `resources/preparation/sources.json` | 研究来源登记；不是当前实施配置 |
| `sessions/` | 按工作批次保存的历史摘要，允许保留当时的旧路径与决定 |
| `skill/` | 项目 Skill 的模板位置；实际实现状态见 STATUS |
| `data_B/` | 本地竞赛数据；Git 忽略 |
| `configs/local-runtime.json` | 本机资源/清理策略；CPU/内存/进程限制对合成 Run、Init、测试、质量和离线 Agent 任务生效，字段语义见设计与契约 |
| `configs/bootstrap.json` | 固定 uv 下载身份、大小上限和项目 Python 版本 |
| `.dfx-local/` | 本机环境、缓存、工作副本与运行状态根；Git 忽略，合成、安装、测试、质量和离线 Agent 任务受管 |
| `runs/` | 历史原型产物、管理工具接入前的临时材料及需保留的 exports；Git 忽略 |
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
| `contracts.py` | 首批 Pydantic 领域记录、可信入口的证据来源标注、引用与行号校验，以及独立动作授权契约；尚未逐调用接入 Agent |
| `workspace.py` | 从已校验分析快照建立独立草稿、原子文本编辑、冻结完整 diff 与候选哈希，并校验恢复归属；不执行应用检查 |
| `models.py` | 固定响应模型经账本接入 Deep Agents 循环，并实施调用/已知 Token/时间预算；真实提供方适配待实现 |
| `model_calls.py` | 持久模型请求账本、响应回放、格式/usage 分类及崩溃窗口拒绝重发；已用于固定响应路径，尚未接入真实提供方 |
| `agent.py` | Deep Agents 离线循环，提供通用读改查、未确认诊断、候选门控、可关闭策略工具和停止报告 |
| `policies.py` | 四类可迁移规则提示、义务/合成反例引用及按修法区分力选择查证的建议；不执行探针或证明应用效果 |
| `acceptance.py` | 候选无关的共同协议、原版/候选执行记录及保守接受门；仅合成契约，不产生应用证据 |
| `verification.py` | 冻结候选专属的离线检查意图、幂等键和合成执行器；无应用成功判定 |
| `backends.py` | 版本绑定的 HTTP 模拟请求/作业对象、客户端意图与查询/取消/产物校验；本机真实执行适配器尚未实现 |
| `remote_service.py` | SQLite 持久化的 FastAPI 模拟作业服务和显式测试工作者状态；保留作协议反例测试，不是计划中的运行服务 |
| `runtime.py` | 当前仅观察 PATH/模块/环境变量存在性；不启动 SDK/设备 |
| `cli.py` | 参数与命令分发；repair / resume 只允许受管固定响应离线演示 |
| `__init__.py` | Python 包入口 |

## 查看与检查

从仓库根目录运行：

```powershell
python scripts/dfx.py --help
.\scripts\local.ps1 Status
.\scripts\local.ps1 Init
.\scripts\local.ps1 Run -Task test -TestPath tests/test_sdk_compat.py
.\scripts\local.ps1 Run -Task docs
.\scripts\test_local_runtime.ps1 -Suite Paths
```

受管 pytest 可运行 `-Task test -TestPath tests/test_agent_loop.py` 检查闭环。受管离线演示的输入约定见 [原型说明](src/dfx_agent/README.md)；它只使用固定响应和合成判据，不启动真实模型或应用。盘点工具用法见其 README，真实任务输入必须显式提供经核验的清单。

## Git 与归档

本地 Git 保存当前源码、模板、文档和历史工作摘要，尚未配置远程。`git status --short` 查看变更，`git log --oneline -5` 查看批次。当前版本只保留一套生效决策，旧版本通过 Git 查询。

失效材料移入 archive 并从 Git 索引移除；目录中的本地说明记录替代入口。新克隆不会含本地归档，当前文档/脚本不得依赖它。历史 session 是已发生行为的记录，旧日期和旧路径不视为当前决策；不为消除历史差异而篡改它们。

新增当前 Markdown 先确认独立职责，再登记；其余细则统一见 [AGENTS](AGENTS.md)。
