# 竞赛与离线修复 Agent 使用说明

状态：当前使用说明。最后维护：2026-10-11。
职责：维护现有命令、输入格式和行为限制；项目进度见 [STATUS](../../docs/STATUS.md)。

当前命令行支持输入快照、日志解码、栈位置到源码映射及离线报告。`repair/resume` 保留固定响应和模拟模型协议，把 Deep Agents SDK、因果诊断与修复义务、有限条件结果、候选工作区、模型/工具账本、合成检查和可关闭策略工具接成受管闭环；受管 aggregate 和 Export/Verify 可汇总并保存既有记录。新增 `competition` 统一主机配置、云端请求适配器、完整工程的本机检查、正式通用领域 Skill 和竞赛交付。统一入口的软件验收证据和未通过项见 [STATUS](../../docs/STATUS.md)。本批只用 MockTransport 与短时合成程序，不运行真实云端或 HarmonyOS 实验；合成 `offline_checked` 或 `synthetic_checked` 不能解释为应用修复成功。公开脱敏、真实环境接纳及修复效果仍待验证。

### 标准用户命令与部署

**以下是产品入口的使用方法；安装和验收状态以 STATUS 为准。** 受管安装必须先通过资源盘点，再实际同步依赖锁并生成 dfx.exe。Windows 当前需要已安装的 PowerShell 7（pwsh）、完整项目目录和项目环境；不是单独 wheel、纯 Python 或跨平台部署。内部管理/工作脚本仍保留，用户无需手写它们的长参数。本机不改全局 PATH、pip 或注册表。

首次安装或清除项目环境后，从项目根目录执行 `setup.cmd`；已有环境且所有构建依赖已缓存时可用 `setup.cmd -OfflineSync`。实际 Init 核 uv/Python、执行 lock/sync、检查 SDK 和已安装 dfx 的 help/version 后才记录环境身份。资源不完整、构建包未缓存或同步失败时必须先处理具体原因，不能改 environment.json 冒充安装。

安装成功后的日常命令如下。PowerShell 在项目目录使用 `.\dfx`，已激活项目虚拟环境时也可使用裸 `dfx`：

```text
.\dfx --demo --name trial01
.\dfx --target reviewed/task01 --config configs/task01.json --name task01
.\dfx --resume task01
.\dfx status task01
.\dfx logs task01 --follow
.\dfx stop task01
.\dfx report task01
.\dfx env status
.\dfx env clean --scope cache
.\dfx env clean --scope cache --apply --plan-id PREVIEW_PLAN_ID
```

`--target` 是已经审核的任务快照目录，含 task.json、source、crash.log 和清单；不是直接传任意原工程即可实验。输入位于部署项目中且不得来自原始 data_B、archive 或指令目录；完整工程、验证命令和业务判据通过 --config 绑定。真实配置尚未接纳时只运行 --demo。省略 --name 时自动生成会话名；工作目录和批准路径自动派生。`--resume NAME` 消费该新运行保存的原位置/profile，并继续原准入和预算检查，不自动替换配置。旧运行没有新位置文件时沿下面兼容入口显式提供参数；源码/依赖变更仍可能使恢复被拒绝，不能通过新前端绕过。

`--workspace` 可显式指定完整部署目录；没有时从当前目录向上寻找，或读取 DFX_PROJECT_ROOT。`--config` 包含 provider/model、应用检查和预算，凭据只通过其中指定的环境名读取，不放命令参数。普通命令不会自动修改真实实验开关。

`working_context.history_selection` 可选 `legacy_refill`（缺省，保留旧计划语义）、`repair_relevance`（按已登记修复引用选择）或 `repair_frontier`（额外保护未吸收探索与换根因后的最近实际失败）。演示配置使用 frontier；其 `exploration_read_groups` 控制可选探索组上限，仍受共同视图预算约束。最新故障日志、必要义务、最近实际失败的原候选登记/检查、尚未冻结的编辑、完整知识读取及近期工具组优先保留，无关旧思考和重复读取可移出。旧候选失败不成为当前候选事实；必要内容超过预算停止，Graph 原历史不删除。新增分支的运行验收状态见 STATUS。`competition-report.json` schema 3 的 `localization_result.final.selected` 是最新候选实际关联的所选根因，alternatives 单列；位置缺项会使最终接受保持 false，实际应用检查结果仍独立保存。非空根因行不表示定位正确。

frontier 可显式设置 `context_layout: "decision_layers"`，以阶段问题、假设/未知、事实/推断、运行条件/业务义务、候选检查和失败修订组织原语义记录，替换默认 `record_panel`。两种布局均冻结到 profile；旧缺省形状不变。编译回组须完整还原原投影，不能只发送摘要却认证原文可见；阶段建议不代替执行权限或最终接受。演示配置与待填写云端模板均显式选择 frontier 和分层布局，新增软件用例及 SDK wire 验收状态见 STATUS。

候选 DeepSeek 协议可在 model 中显式写 `thinking_mode: "disabled"` 以测试有限历史；云端模板明确选择此准备配置，适配器会发送 `thinking: {"type":"disabled"}`，必须先核对实际提供方支持该字段与模式。它不是所有 OpenAI-compatible 服务通用的参数；其他提供方须按实际协议审查后配置，不能凭模板假定兼容。思考模式写 `thinking_mode: "enabled"`，可配 `reasoning_effort: "low"/"high"/"max"`，并使用完整历史（working_context.enabled=false、不启用主机压缩）。enabled 不接 temperature 或强制工具选择，历史 reasoning 缺项/裁剪会在发送前停止。未声明模式仍是旧通用配置语义，不能据此判断提供方的实际默认。协议适配源码和 Mock 用例的软件验收结果见 STATUS；尚未请求真实服务，最终模式由接纳和同模式比较决定，有限历史准备配置不代表已决定比赛最终模式。

运行中显示实际模型派发/回执、工具授权/结果保存、原版和候选的 build/target/behavior/regression 阶段、源码绑定观察以及最终报告。工具结果保存不是业务通过，阶段退出成功不等于修复被接受；定位推断及完整补丁通过 result.md 查看。默认文本逐条反馈；`--output-format json` 只输出一个最终结果/观察，`stream-json` 每个事件一行，可用于其他界面。`logs NAME --after N --follow --output-format stream-json` 接续序号；查看日志时 Ctrl+C 只退出查看，运行时 Ctrl+C 请求停止本次 manager。缺身份或停止未确认保持 unknown，不停止其他任务。

events.jsonl 是容量有限的操作侧录：不输出模型正文、源码、日志正文、工具参数或凭据，不展示虚构百分比/ETA。到限、损坏或缺失会声明 truncated/unknown/unavailable；原账本和最终接受仍独立。CLI status/report 是运行视图，不能代替原件核验或效果指标；当前摘要读取拒绝超过 32 MiB 的完整报告，拒绝展示不更改其原始结果或应用接受。查看旧运行不补造实时事件时间；私有 delivery.zip 保存打包时的事件快照，打包成功后的完成事件仍可能仅在 work 中。

退出码：0 为管理/查看完成，或 Agent 获得应用接受；2 为 Agent 已结束但未获得应用接受（含 demo 合成通过）；1 为错误或管理状态未知；130 为中断。因此 demo 正常完成时退出 2，report 的退出 0 不表示其 accepted=true。

### 兼容管理入口与竞赛输出

以下底层入口保留用于开发检查、显式旧记录恢复和兼容；需要 PowerShell 7，从仓库根目录执行：

```powershell
.\scripts\local.ps1 Run -Task competition -CompetitionConfigPath configs/competition-demo.json -CompetitionWorkId demo01
.\scripts\local.ps1 Run -Task competition -CompetitionConfigPath configs/competition-demo.json -CompetitionWorkId demo01 -Resume
.\scripts\local.ps1 Status
.\scripts\local.ps1 Stop
.\scripts\local.ps1 Clean -Scope cache
```

`mock_demo` 自建合成任务和完整玩具工程，不传 `-CompetitionTaskPath`。它用脚本响应走同一 CloudChatCompletionsAdapter/SDK 调用通道，并由短时本机程序提供失败后修订和四项检查的合成反馈。演示中的定位、补丁、usage 和检查都不是真实模型或应用效果；即使合成检查通过，仍保持 `accepted=false`、`application_verification=not_run`。实际软件验收结果只见 STATUS。

工作目录固定为 `.dfx-local/work/competition-<ID>/`，新运行不能覆盖旧目录。控制台只打印 schema、status、accepted、application_verification、候选数、模型调用数和报告路径等简短概要，不打印原始输入、模型请求或凭据。输出职责如下：

| 产物 | 用途与边界 |
|---|---|
| `competition-report.json` | schema 3 竞赛结构化结果：初次与最新候选关联的所选根因/备选、全部候选/补丁与检查、独立的实际检查层级/结果和任务接受、失败反馈/修订、停止原因、成本、固定终态观察计时、来源及未知指标；旧报告仍按原代读取 |
| `result.md` | 面向审阅者的定位、修复、验证和限制说明；控制台 report_path 指向此文件 |
| `delivery.zip` | 未脱敏的私有交付原件及字节/哈希清单，供保留和审阅；字节核验不等于评测重聚合、根因认证或修复成功证明 |
| `agent-report.json`、账本及 Graph 检查点 | SDK 运行轨迹与恢复依据；agent-report 不是最终竞赛接受报告 |
| `events.jsonl` | 新运行的有限实时操作观察；不充作接受、成本或历史时间证据 |
| `product-config.json`、`product-launch.json` | 新前端恢复时定位原配置和任务；真正准入仍由原固定事实与外部批准决定 |

`configs/competition-demo.json` 是受管软件演示配置；`configs/competition-cloud.template.json` 说明后续真实提供方与工程接纳字段，包含待填写值，不能直接作为当前实验配置。本机策略 `configs/local-runtime.json` 的 `allow_model_experiments` 和 `allow_application_experiments` 均保持 false，本批不得通过环境变量或配置修改解锁真实云端、HarmonyOS 构建或设备运行。

真实模式须由主机先审阅任务快照、完整工程及业务判据，再固定配置 `execution` 中的 `project_root`、`source_mapping` 和 `commands`。分析快照排除了运行资源，不能拿它充当完整工程。映射为空时按相同相对路径绑定；非空映射须覆盖分析基线的全部源码，原字节必须匹配。四个固定命令键为 `build`、`target`、`behavior`、`regression`；原版先完成构建和指定故障复现，候选再完成这四项检查。实际应用协议须固定故障标记及各命令 oracle 哈希；模型不能改写 oracle、工程路径、命令或接受条件。环境和业务协议的接纳不等于已通过应用验证。

CloudProviderConfig 只保存 HTTPS endpoint 与凭据环境名；endpoint 不允许 userinfo、query 或 fragment，凭据在准入及发送时从环境读取，不保存到 profile、请求体、异常或日志。本机应用子进程排除模型凭据环境变量。Cloud 适配器共用原严格序列化/解析器，显式网络准入后只发一次请求，禁用自动重试、重定向和环境代理并限制响应大小；本批所有调用仍使用 MockTransport。发送前预留保守输入估计与最坏输出 Token，超时受剩余任务时间约束，未知 usage 后停止新发送；这一估计不是供应商 Tokenizer 的精确硬上限，型号兼容性仍待实测。

四份正式过程为 `resources/skills/bounds.json`、`lifecycle.json`、`recursion.json`、`dynamic_pattern.json`，分别针对边界、生命周期、递归进展和动态模式。每份 ExperienceContent 保存 trigger、premises/unknown_question、procedure/branches、preserves、counterexamples 和 exit_conditions；提供完整诊断修复过程，不含 data_B 实际答案或任务身份，也不声称收益。competition 以主机批准冻结其原字节、知识快照与静态选择，复用 search_experience、三态交接及 read_skill，不自动学习或晋升。

`-Resume` 必须沿用相同工作 ID、任务和配置，保留原完整工程、work 内全部原件及 `.dfx-local/state/competition-<ID>-approval.json` 的原批准。恢复消费 run 内原知识和静态资源，不能重建缺失工程、重新生成批准或把新资源加入旧运行。执行源码/依赖身份、配置、工程、映射、协议和批准有漂移时拒绝；恢复准入拒绝返回 3 并保留原终态。已完成模型/检查调用只回放结果，已发出但结果未知的调用停止并不重发。无待执行工作的完成恢复即使用满额度也保留原结果和终态观察计时；仍有待执行工作时不能重置原预算/截止时间。旧报告没有可信完成时间且无新进展时耗时保持未知，不能把空闲和恢复时间算成旧修复耗时；首次报告观察区间也不等于定位/修复阶段耗时。字段与旧代回读边界见[规格第 1.3.1 节](../../docs/design-contracts.md#131-案例接纳与协议冻结)，实际通过范围见 STATUS。

Stop 终止当前受管 Job，Status 查看状态。Clean 默认只预览，示例仅预览缓存；预览 work 可用 `Clean -Scope work`，保留条件、PlanId 和显式丢弃要求沿用 [本机管理协议](../../docs/development/system-design-draft.md#9-本机环境资源限制与集中清理)。需要恢复时须保留原工程、任务、work 与 state 原批准；mock_demo 的原工程和任务位于 `.dfx-local/tmp/`，不能先清掉再续跑。交付包不会自动授权清理，也不能代替恢复原件。

### 既有离线证据与脚本入口

从项目根目录查看受管状态并运行合成自检：

```powershell
.\scripts\local.ps1 Status
.\scripts\local.ps1 Run -Task test -TestPath tests/test_agent_loop.py
```

`working_context.py` 提供独立 packet 接口：显式 WorkingContextFocus 生成来源/版本已核验的引用闭包，verify_working_context 只在同一来源点回读；它本身不是历史恢复器。`working_context_plan.py` 则接入实际 SDK 模型边界，从当前可见的授权工具回执取得焦点，冻结原消息、来源原字节、完整组选择和发送回执。必要记录附入启动任务的引用数据区，随后可独立启用通用压缩；原 Graph 历史保持完整。旧计划按物化材料回读，后来合法完成检查不会改变过去输入；必要包或视图超字节预算明确停止。字段及边界见 [模块规格第 3.1 节](../../docs/design-contracts.md#31-工作上下文经验与-skill-的实施契约)。独立 packet 的合成自检入口为：

```powershell
.\scripts\local.ps1 Run -Task test -TestPath tests/test_working_context.py
```

`prepare` 必须显式提供经人工核对的 JSON catalog、task ID、数据根目录和新的输出目录。本阶段只筛出 021/022 两项待接入候选，尚未形成可运行的正式 catalog。当前 catalog schema 2 的最小结构如下；这些是字段示例，不是可运行的真实路径或哈希：

```json
{
  "schema_version": 2,
  "cases": [{
    "task_id": "example",
    "case_key": "group/example-case",
    "lineage_group_id": "example-family",
    "fault_variant_id": "example-fault",
    "app": "relative/application/directory",
    "log": {
      "path": "relative/crash.log",
      "sha256": "REPLACE_WITH_SHA256_OF_LOG_BYTES"
    },
    "scenario": {
      "path": "relative/evaluator-only-scenario.py",
      "sha256": "REPLACE_WITH_SHA256_OF_SCENARIO_BYTES"
    }
  }]
}
```

ZIP 内日志还必须给 `log.member`，哈希对应成员解压后的原始字节。路径均相对于 `--data-root` 且使用 `/`；`scenario` 可省略，但存在时只做原字节哈希核验，绝不复制进 Agent 输入。catalog 拒绝重复 task ID、案例键和同字节日志，不能用重复材料抬高样本量；案例键和谱系由准备者核对，字段本身不证明日志、源码与实际构建版本一致。`prepare` 产出的 task schema 2 只暴露案例选择摘要、日志和 `analysis_source_v1` 源码快照；旧 schema 1 合成快照仍可读取，不是正式案例入口。该快照会排除运行资源和测试材料，不是可构建的应用包，也不是操作系统访问隔离机制。

`analyze` 接收含 `task.json` 的快照目录，输出目录必须在该快照之外。报告保存栈位置、相关源码和映射歧义；`root_cause` 与 `patch` 为 null，验证状态保持 not_run。输入哈希校验能发现相对清单的改动，不证明清单的来源可信或日志与源码运行版本一致。

离线演示必须通过受管入口执行，输入已准备并通过 `verify_task` 的任务快照、JSON 脚本和新的工作 ID：

```powershell
.\scripts\local.ps1 Run -Task offline -OfflineTaskPath <快照目录> -OfflineScriptPath <固定响应JSON> -OfflineWorkId demo1
.\scripts\local.ps1 Run -Task offline -OfflineTaskPath <快照目录> -OfflineScriptPath <固定响应JSON> -OfflineWorkId demo1 -Resume
```

`<...>` 是要替换的参数示意，不是可直接运行的真实任务。输入路径须在仓库内且不在 `data_B/` 或 `archive/`；工作目录固定为 `.dfx-local/work/offline-<ID>/`。schema 1 脚本格式是 `{"schema_version":1,"responses":[{"tool":"edit_source","args":{"path":"src/main.ets","old":"bad","new":"good"},"usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15}}],"expected":{"path":"src/main.ets","text":"value=good\n"}}`。`tool:null` 表示结束消息，此时 args 必须为空；`usage:null` 表示用量未知，存在的用量须为非负整数且总数一致。

脚本 schema 1/2 分别用于固定响应与模拟 wire；保持工作上下文关闭。schema 3/4 保持各自响应结构，必须另提供 `working_context`，例如 `{"enabled":true}`；其全部策略参数会展开冻结在 session，不能靠旧脚本的额外字段悄悄启用。schema 5/6 分别在固定/模拟结构上必填 `working_context` 和主机 `knowledge` 配置。schema 2/4/6 用于检查实际模型协议交接，仍是进程内模拟，不连接云端：

```json
{
  "schema_version": 2,
  "model": {
    "mode": "mock_chat_completions",
    "settings": {"provider_id": "mock-provider", "model_id": "synthetic-model", "max_output_tokens": 128},
    "responses": [{"id": "reply-1", "model": "synthetic-model", "choices": [
      {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "done"}}
    ], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}]
  },
  "expected": {"path": "src/main.ets", "text": "value=good\n"},
  "limits": {"max_calls": 6}
}
```

两种格式均可选 `policy_features`（rule_guidance、obligation_tracking、decision_checks 三个布尔开关，缺省全关）和 `limits`（max_calls、max_known_tokens、max_seconds、max_candidates、max_tool_calls，正数，未指定时沿用循环默认值）。文件最多 1 MiB，拒绝链接、额外字段、重复 JSON 键和非有限数值。既有离线脚本配置不接受密钥、端点或真实网络模式；responses 与 expected 都不进入模型任务上下文，expected 只供合成执行器。模拟恢复按已完成调用数对齐响应流；完成请求回放不消费新响应，已发出但未知的请求不重发。已正常结束的 Graph 再 resume 只核对/汇总，不开启新模型对话；已用尽预算仍明确报告耗尽。修改模型配置或循环限额不能继续原会话。

schema 5/6 的 `knowledge` 是主机配置，模型不能提交或扩大它：

| 字段 | 内容 |
|---|---|
| `library_path` | 仓库根下已经批准、物化快照并建好索引的 KnowledgeStore 相对路径；与任务和输出目录分离，不能指向 data_B、archive 或内部工具目录 |
| `approval` | 完整 TaskKnowledgeBinding：schema_version、task_id、run_id、source_snapshot_sha256、crash_sha256、snapshot_sha256 和 scope；由主机独立批准，不从 run 配置自动生成 |
| `applicability_policy`（可选） | 完整 schema_version=1 和 enabled 布尔值；显式提供时两组使用相同三态工具 |
| `static_skill_policy`（可选） | 完整 schema_version=1、enabled 和升序唯一 entry_ids；必须同时显式提供三态策略，关闭组仍能完整 read_skill |

受管 worker 自动传仓库根，启动先只读核知识原件、索引、权限及任务批准，缺库不会创建空库或重建索引。恢复读取 run 内冻结原件，并要求脚本提供原批准和原策略；库可以已经迁移或不存在，`library_path` 仅是启动定位符，不参与恢复身份，但仍须满足词法范围约束。批准、知识、策略、选择、模型和预算的漂移在可写运行前拒绝，CLI 返回 3，worker 保留原终态。库制作与人工批准继续使用 knowledge.py/knowledge_retrieval.py 的主机接口，本入口只消费已有库。完整受管验收 fixture 见 tests/test_managed_knowledge_workflow.py。

每次 SDK 工具调用先经 `actions.py` 的主机授权：检查工具对应动作、参数、任务/版本、引用、候选及能力；全部动作串行，并受独立工具预算约束。工具参数可带 `proposal_json`（序列化的 `ActionProposal`），用于显式提交理由和引用；它不能改变工具所需能力或候选，也不能隐藏工具实际引用。模型返回的工具调用并不自动获准；最后一个已记账模型响应仍可完成合法动作，但不能绕过工具、候选和时间预算。

`task_strategy.py` 已向 SDK 注入明确的任务指引：读日志、追查跨文件的上游值与消费者、提出原始行假设、生成保行为补丁、冻结候选、按实际检查反馈修订或诚实停止。基础/增强共享该流程，增强只按开关追加规则、义务和决策检查。启动消息绑定主机任务、版本、能力与合成验证级别；脚本响应自检能证明交接，不能证明模型自主定位或修复效果。

`search_source(query=..., path_prefix=..., candidate_id=..., offset=..., limit=...)` 可快速找符号定义、赋值与调用文本，默认搜索原版；指定已登记 candidate_id 可查冻结候选，不能查草稿。查询是单行字面文本，前缀是相对文件或目录、不展开 glob。结果有实际源码版本、路径、行列、原字节哈希与有限片段，分页由 has_more/next_offset 指示；complete/skipped 明确编码覆盖。片段截断不能代替完整上下文，缺编码文件时不能声称不存在匹配。整树/单文件/查询/页面限额来自 source_search.py，超过即拒绝；工具共用 read_source 能力和总预算。它只查文本，不确认数据流或使用点运行值。

`read_source(path=..., start_line=..., end_line=...)` 默认按行读原版，`draft=True` 读未冻结草稿，`candidate_id=...` 读已登记冻结候选；草稿与候选不能同传，每次最多 200 行。失败修订可以先在原候选检索，再回读完整行段，对照实际检查反馈；候选回执标明候选及源码快照，不能当作原版根因行。文本编辑仍用 `edit_source(path, old, new)`，其替换执行委托 SDK，全部成功后切换临时草稿。编辑文件规范为 LF，末尾换行有无保留，原版/已登记候选不受改写。新读取/委托源码和反例均待受管验收，见 STATUS。

结构化诊断使用 `inspect_analysis(include_schema=True)` 读取主机身份、提交 schema 和接纳规则，再把序列化的 DiagnosisSubmission 传给 `record_diagnosis(diagnosis_json=...)`。它包含异常/根因位置、条件与绑定、因果步骤、支持/反驳引用、未知项及可选义务；源码和日志事实由主机读取，模型只能提交推断。义务需要开启 `obligation_tracking`，并和假设双向关联。简单 path/line/reason 路径仍可用，不能与 diagnosis_json 同传。

`diagnoses/<ID>.json` 原子保存诊断、证据和关联义务，不能改写；修订用新 ID。已有证据可按 ID 复用，依据闭包保留原来源。`inspect_analysis(diagnosis_id=...)` 回读同一记录，默认不重复返回 schema。独立 `record_obligation` 也使用规范 RepairObligation；候选的 restores/preserves 引用上述义务。模型假设、关系和义务保持 unknown/model_inference；失败执行依据须匹配已保存检查的候选、实际源码哈希和 execution_id，不能把旧候选反馈移到新候选。完整字段与未实现检查见 [模块规格第 1 节](../../docs/design-contracts.md#1-数据契约与不可破坏的条件)。

`plan_repair_action(plan_json=...)` 现在接收序列化的 DecisionRequest：task_id、run_id、diagnosis_id 和 plan（PolicyInput）。方向必须引用该诊断的 hypothesis_ids、support_evidence_ids 和相关 obligation_ids；检查映射须与已登记区分问题对应。include_schema=True 同时返回 decision_request_schema。主机覆盖自报能力和预算，只建议直接修、已有证据补查或带未知尝试，不执行探针。单独的旧 PolicyInput JSON 不再作为此工具输入。

失败后 `link_failed_check` 返回反馈及 feedback_id，再决策时成对传该 feedback_id 与原 candidate_id；主机重新核对已保存失败检查、候选源码哈希和原观察。`decisions/<ID>.json` 保存建议及上下文摘要；`inspect_analysis(decision_id=...)` 可回读，默认列出决策和反馈 ID。`register_candidate` 可选成对传 decision_id/direction_id，核对同诊断、所选提议方向及声明义务；建议不强迫模型采用某一修法。失败与义务的关联仍是推断，只建议重审相关方向。

`agent-report.json` 是 SDK 轨迹与摘要。其无知识 schema 7/completion 4 绑定 session 10 中保存的 execution_provenance；主机明确批准知识时使用 11/8/5，另保存 knowledge_profile 和 knowledge_facts。显式三态启停策略使用 12/9/6，另保存 applicability_profile、applicability_facts 和 solver-inputs 中的实际发送记录；显式静态启停使用 13/10/7，另保存 static_skill_profile/static_skill_facts，分别记录完整读取与后续实际可见性。批准版本程序入口使用 14/11/8 和 version_skill_profile/version_skill_facts，原代保持原形状。记录候选、检查、已保存决策、condition_results、模型与工具调用、授权/拒绝、结果哈希、策略开关和未知状态。旧 session 8/report 5/completion 2 与 9/6/3 保持原形状只读回读，拒绝跨版本拼接。摘要区分首次允许编辑前的初次定位、最新定位及最新候选关联定位；定位始终是原始源码坐标的模型推断。candidate_artifacts 列出完整候选、相对 patch_path、实际哈希和检查；latest_candidate_check_status 与历史通过列表分开，最新失败/未检查不会被旧通过覆盖。working_context_plans 记录可见动作水位、组织视图/实际发送哈希、选择范围与保留成本；context_compactions 记录摘要来源、响应与实际 solver 视图的核验状态，摘要不能覆盖检查事实。cost 记录完整调用分母及 calls_by_kind，缺失/无效/未知用量不补零；缺少计时依据的定位/修复耗时为 null。该 SDK 摘要的 accepted 保持 false；competition 的接受状态另由完整应用检查事实生成在 competition-report 中，不能拿 agent-report 的合成摘要作竞赛成功判定。

`reporting.read_completion(workspace, ledger, model_calls, actions)` 是程序回读接口：重新核对源码、补丁、诊断、模型/工具请求与回执、条件、决策和 session，拒绝与这些事实矛盾的摘要或展示字段；不重新调用模型或执行检查。知识运行必须另传主机实际 `task_directory=task`，不从模型字段取外部路径；build_completion 同样要求它。report_validation 明列核验范围、用于重算的停止原因和无法回读重建的本轮计数/适配诊断，摘要哈希不代替独立来源可信性。报告文件与候选 diff 可直接审阅；批量回读使用下述 aggregate，保留完整核验依据使用受管私有 Export。

### 只读聚合已停止的合成记录

```powershell
.\scripts\local.ps1 Run -Task aggregate -EvaluationManifestPath <仓库相对清单.json> -EvaluationWorkId <新ID>
```

完整报告写入 `.dfx-local/work/aggregate-<ID>/aggregate-report.json`，终态写入同目录 run-state.json，控制台只打印小回执。任务沿用现有 Windows Job、串行入口及资源限制，不调用模型、执行工具/检查或读取固定脚本的答案。旧目录不覆盖，不支持 Resume；强制中断后若无终态，清理保持拒绝。格式与程序接口在 metrics.py，自检样例在 tests/test_metrics.py；指标及未知项口径见 [评测说明](../../docs/preparation/metrics-and-gates.md#当前离线聚合的口径)。

清单 schema 1 的必需字段：evaluation_id、scope=`offline_synthetic`、configuration、tasks、variants、repeat_ids、runs。tasks 列出 task_id/case_key/lineage_group_id/fault_variant_id、task_path、task.json 原字节 task_sha256、source_snapshot_sha256/crash_sha256/protocol_sha256/observations_sha256；task schema 2 还提供原 CaseSelection，task schema 1 selection=null。variants 每项为 variant_id 和完整 PolicyFeatures。runs 每项为 task_id/variant_id/repeat_id/run_id/work_path，必须覆盖全部矩阵；work_path=null 表示未运行。输入路径为相对于仓库的规范路径，不能指向 data_B、archive 或内部 Git/助手目录。

清单 schema 1 的 configuration 中 model_profile_sha256、context_profile_sha256 和 search_limits_sha256 分别取 `sha256(canonical(session[字段]))`，tool_schema_sha256 取 session 同名值，再列出共同 capabilities/max_calls/max_known_tokens/max_seconds/max_candidates/max_tool_calls。canonical/sha256 由 model_calls.py 提供；不要对随意缩进或不同排序的 JSON 直接取这些配置哈希。聚合会重算任务提示及实际主机/solver 工具定义，核对所有 session 与同组预算；旧普通 solver 原始请求体未保存，不声称完整历史请求重建；新运行启用工作上下文时另外核验保存的完整发送回执。

清单 schema 2 用于显式上下文对照：configuration 另给 `working_context={max_packet_bytes,max_view_bytes,retain_groups}`，variants 每项必有 `working_context_enabled`；这些限制仍共享。此时上下文配置哈希取 `sha256(canonical(metrics.shared_context_profile(session["context_profile"])))`，只将 enabled 归一为 false，保留压缩及其他参数。清单 schema 3 继承该结构，configuration 另给 `knowledge_profile=knowledge_run.shared_knowledge_profile(session["knowledge_profile"])`，固定全部组共用的快照、scope、retriever、索引哈希及知识原件清单；排除 task/run 特有批准，但不放宽知识或工具。该代支持普通检索；schema 4 继承同一共享配置，variants 另必填 applicability_enabled，用 session 12 的同工具启停比较，不接纳学习因素；schema 5 另冻结 shared_static_skill_profile 和 static_skills_enabled，以 session 13 比较相同静态资源的目录启停。清单 6 使用 shared_version_skill_profile/version_skills_enabled 与 session 14，比较相同获准版本池的目录启停或事前目标过程，完整学习因素另待 ResearchProfile。`parse_evaluation_manifest` 显式区分六代，不给旧清单倒填默认值；1/2 拒绝知识运行，1 还拒绝开启工作上下文的运行。脚本、会话和评测清单的 schema 各自独立，脚本 5/6 按实际选择生成会话 11、12 或 13。

新运行的 context_profile 冻结 working_context_format=2：plan schema 2 以路径、摘要和大小引用同 run 的 working-context/materials 原字节，重复材料只保存一次；完整审计 packet 仍保留逐文件来源。模型投影只将 sources 清单替换为有摘要的统计，保留其他领域内容全文，覆盖判断使用实际投影。材料/审计产物沿用 MAX_ARTIFACT_BYTES 单产物限额，WorkingContextPolicy.max_packet_bytes 限制求解投影；超限明确停止，不放大配置。旧 session 10 缺标记时按 format 1 只读核验原 inline 计划和原预算解释；旧报告/包摘要不倒填，来源不兼容不能续跑。

原材料先于计划发布，崩溃可留下未引用完整 blob 或有界 pending，审计只读保留并计数，不自动补引用或清理。单独 Plan/Pydantic 校验仅确认结构，外部原字节须经 WorkingContextStore 回读/绑定/审计核验。working_context_plans 的 materialized_bytes 是逻辑引用量，unique_referenced_material_bytes 按摘要去重；run_material_observation 是整个 run 的累计 namespace 观察，在各计划重复展示，不能求和为每调用开销。solver_projection_bytes、stage/final_bytes 是 UTF-8 字节，真实模型 usage 仍单独记账。规模自检不表示已适配 data_B 全部工程或有修复/Token 收益；实际边界与下一工作包见 STATUS。

缺目录、缺报告、失败和未知成本均保留在分母；损坏证据拒绝整批，不跳过。首次与最新定位/候选分开，repeat 与任务/谱系分开，不宣称独立样本。所有应用指标为 null、accepted=false。全旧 session 8 数据维持原汇总形状及历史版本 null；存在新来源时，逐行保留原记录，按 compatibility_sha256 列出来源与未知项，已知兼容身份不一致拒绝合并。缺报告的新来源仍须绑定匹配工作区和会话，不能据此补零费用或成功。从现有记录生成清单不能证明当时预登记，正式实验须先冻结清单与完整应用执行来源。程序 API 为 load_evaluation_manifest / aggregate_evaluation / write_evaluation_report；写入只允许新报告且不能与输入重叠。

`run-state.json` 记录受管工作进程终态。`model-calls.sqlite` 保存请求/响应账本，`tool-actions.sqlite` 保存授权与完整工具结果，Graph 检查点和检查意图共同支持恢复。已完成工具在检查点落盘前中断时只回放结果；工具执行过但回执未保存时停止为 `action_result_unknown`，不盲目重做。恢复沿用原 ID、预算、主机能力、策略开关、条件输入指纹、项目任务提示、初始上下文、工具定义、检索限额、model_profile 和 context_profile，以及 session 10–14 的兼容来源身份（此代版本入口为程序 API，受管 CLI 保持已接入的 10–13 范围）。旧 schema 1–9 不续跑，不自动升级，须保留证据并另建工作 ID。来源准入拒绝时 CLI 返回 3，工作进程保留原终态；已准入后运行失败仍记录 failed。Graph 只保存连续 solver model-N，摘要使用独立身份，不能用总调用数恢复 solver 序号。被中断且尚无终态的工作目录会阻止清理，须先核对再恢复或处置。

### 开发反馈与待审核经验：程序接口

模型调用账本保持分代：求解会话仍用 ModelCallIntent/schema 1；独立学习用 ModelCallIntentV2/schema 2，冻结学习 owner 和来源，experience_extraction 与 learning_format_repair 共享同 operation/run 预算。格式修复须引用同模型/来源的已完成父响应，完整父链和 usage 重核；未知发送不重试，完成只回放。invoke_accounted 的显式 preflight 在 reserve 后、dispatch 前执行，失败保留未发送预约且占额度；实际费用和总 Token 硬限制未接入。它是底层调用状态接口，来源权限仍须由学习工作流核验，不能仅凭哈希准入材料。合成检查入口是 tests/test_learning_model_calls.py，真实实验仍禁止。

`feedback.py` 的 FeedbackStore 消费主机明确批准的单开发运行清单，捕获并独立重核反馈；当前只提供程序接口，须在受管工作流或软件自检内调用，尚无用户学习 CLI。预创建的 Store 位于项目受管目录，不从数据包扫描经验或自动启动模型。

1. 用 HostFeedbackApproval 批准完整 manifest 身份、development 学习用途、允许部署分区及排除谱系。清单只能包含一任务、一变体、一 repeat 和一 run，必须有与 task schema 2 一致的完整 CaseSelection。
2. `capture(manifest, evidence_root, approval=...)` 保存 FeedbackCapsule。原终态、候选、检查、报告、模型/动作账本和知识来源权限都要可核验，原端和副本重聚合结果相同。完整私有原件和空目录清单保留；Graph 不参与也不执行。
3. `read_capsule(capsule_id, approval=...)` 从原件重建，无需原任务、run 或知识库；损坏、缺失和不合格来源拒绝，读取不创建资源或修复数据库。
4. 主机提供完整 SolverProjection（host_curated、validation_scopes 为空），用 HostCandidateApproval 绑定 capsule 与投影身份，再调用 `propose_candidate(..., feedback_approval=...)`。`read_candidate` 需要重传这两份外部批准并核原 capsule。

人工候选始终 pending_host_review、application_qualification=none；前提、未知问题、步骤、保留行为、反例和退出条件完整保存。原修复成本与新候选提议分开，失败和未知不剔除、不补零；该人工接口未调用学习模型，not_run/null 不能写成自动总结或零成本学习。FeedbackLimits 约束单次 API 的原件复制/关闭核验累计读量、文件数和容量；元数据、原件及各回读阶段共用预算，不覆盖聚合内部全部 I/O。旧 KnowledgeStore 条目不变，此接口不自动入库、晋升或修改在跑任务。人工候选完整原件可由下文独立 Skill 版本/验证及沙盒演进消费；独立提炼仍仅使用模拟程序入口，不因竞赛求解的 Cloud 软件通道而获得真实实验授权。模型来源版本经独立程序入口提供，完整整链验收见 STATUS，边界见 [模块规格第 3.1.2 节](../../docs/design-contracts.md#312-经验检索和适用性交接)。

### 独立模型提炼与待审核草案：仅模拟程序接口

`LearningStore` 使用预建的独立受管目录和原 FeedbackStore。主机先通过 `learning_root_binding(root)` 只读固定真实路径及物理目录身份，再给出完整 HostLearningApproval：原 capsule/反馈批准、独立 operation/run、模型设置及本操作预算。构造、summary/read_draft 不创建目录或重建缺失账本；尚无学习 CLI，也不能用此接口连接云端。

1. `invoke(call_id, approval=..., adapter=...)` 仅接 exact ChatCompletionsAdapter/MockTransport。先核全部原件和知识祖先谱系，再发送完整聚合、清单、权限、原日志及全部已登记候选 diff 正文；它们是数据，未授工具能力。完整 PreparedRequest 和响应随账本保存。未发送完整工程源码，不保证片段足以解释所有业务语义。
2. 输出格式错误仍占用次数并保存 usage；只在主机显式提供 `repair_of_call_id` 时另发格式修复，引用原完成响应。合法输出不能伪装格式修复，新 ID 不能重复相同完整请求。发送前 preflight 失败保留 reserved，可核原批准后续发同 ID；发送后 unknown 不重发，完成同 ID 只回放。
3. `propose_draft(call_id, approval=...)` 将合法 ExperienceContent 保存为完整 LearningDraft：model_inference、无验证范围、pending_host_review。它保留来源权限、全部声明学习谱系、完整调用/响应和冻结成本前缀，不成为人工 ExperienceCandidate、SkillVersion 或已启用知识。`read_draft(draft_id, approval=...)` 只读重核原件；主机精确审核后可经下述模型版本接口接入；不能直接传给旧 host_curated 版本接口，旧 KnowledgeStore 入库未接入。

提炼和格式修复共享本操作调用/已知 Token 预算；未知发送、缺失或无效 usage 使总 Token 保持 null，已知和只是下界，实际费用始终未知。summary 也核验已有草案，缺原 capsule、缺账本、删行、改请求或换物理根均拒绝。初始化的跨文件中断失配不自动修复，尚无主机对账入口或独立不可删除日志。容量门预留有限模拟响应及 SQLite 开销，不能据此声称全内部 I/O、峰值内存或跨操作总预算已经闭合。契约见模块规格第 3.1 节，实际验收见 STATUS。

需要保留可移植来源时，主机在另一个预建受管目录构造 `LearningSourceStore(root)`，调用 `capture(learning_store, draft_id, approval=original_learning_approval)`。它复制完整学习操作和反馈闭包，冻结草案原成本前缀以及复制时的全操作成本；未发预约或仍在派发的操作不能复制。随后 `read_source(source_id, approval=original_learning_approval)` / `read_draft(...)` 只读从副本重新核验全部原件，原学习库/反馈库不再可用时仍能回读。包无 invoke 或新预算，不能从复制目录续发原调用；原 `LearningStore` 的执行恢复仍要求原物理根及 capsule。来源移植不表示内容验证有效或已部署，测试和实际完成见 STATUS。

### 运行来源与恢复兼容

`provenance.py` 在新运行创建工作目录前读取受限主机源码清单、uv.lock、pyproject.toml、.python-version 与本机配置，记录文件大小/原字节哈希、实际 Python 标识和安装依赖的 distribution metadata 版本。标准入口在导入 CLI 前冻结主机源码基线，导入后及运行前后复核。Git commit、dirty 和状态摘要只保存初始观察；续跑比较源码、仓库输入和实际环境组成的 compatibility_sha256，不因只改文档/测试或纯文档提交拒绝，也不覆盖原 Git 事实。

恢复来源预检早于可写账本、条件登记、Graph 和模型调用；mock 响应偏移用只读账本取得。记录分层核验源码清单、兼容摘要与完整 provenance_sha256，自洽不等于认证制作者。只读报告/聚合/Verify 不捕获当前环境来回填历史来源；W10 包原样携带嵌入的 session/report，无需新 sidecar。清单不保存主机源码全文或未提交 diff，依赖版本不证明已加载二进制完整性；快照不锁住外部写入者。正式实测仍须提供冻结的主机实现、安装环境和应用工具链/设备依据。

### 私有证据导出：预览、应用、包内核验

使用与 aggregate 相同的显式 EvaluationManifest，只处理已停止的合成记录。以下参数占位须替换为现有仓库相对清单、新 Export ID 和预览返回的 PlanId：

```powershell
.\scripts\local.ps1 Export -EvaluationManifestPath <仓库相对清单.json> -ExportId <新ID>
.\scripts\local.ps1 Export -EvaluationManifestPath <仓库相对清单.json> -ExportId <新ID> -Apply -PlanId <预览返回的HASH>
.\scripts\local.ps1 Export -ExportId <已有ID> -Verify
```

第一条只预览并保存 `.dfx-local/state/export-plan-<ID>.json`，控制台返回有限的计划身份、预计字节和私有内容说明。合法的同 ID 旧计划可以重新预览刷新；未知已有文件、与 manifest/.new 或输入重叠的计划拒绝。Apply 重核原 manifest 字节、owner/config、矩阵、文件身份/哈希及缺失状态，任何漂移都要求重新预览；已有最终包不覆盖，须另选 ID。三个命令使用现有 Windows Job/串行管理入口，不直接运行 Python 导出命令绕过保护。

最终包固定在 `runs/exports/<ID>/`：evaluation.json 保留 manifest 原字节，aggregate-report.json 保存完整源端摘要，package.json 保存计划、隐私说明及字节清单；payload/ 镜像原 task_path/work_path。它完整保留任务源码/日志、基准/草稿、全部候选/diff、Graph、模型/动作账本、检查、诊断/义务/决策、条件/采值、压缩及失败记录。空目录也在清单内，未运行和缺报告格子保留分母及未知费用，不为缺失 work 创建空壳。只复制已登记的明确范围，未知顶层、链接/共享文件、凭据类材料、活动/未知作业、未终态或非空 SQLite WAL/journal 均拒绝；不自动恢复数据库。

复制先进入受管 tmp 的独立 ex- 前缀暂存。单包上限和 managed/exports 共同磁盘预算以 configs/local-runtime.json 为唯一来源；复制前预估，过程检查磁盘与新增量，完工重扫。源树不变、清单及包内重聚合全文一致后才发布新目录；失败暂存未发布，源输入保留。Verify 的证据输入只依赖包内字节，重新核验全部文件、只读 SQLite 与摘要，不读取原输入，不执行模型、工具、检查或 Graph 反序列化。Graph 只证明已保存字节/数据库完整性，没有验证其可恢复执行。

Verify 仍须使用当前已初始化的受管环境，核对其 owner、依赖锁和自身租约。包中 owner_id 记录导出来源实例，不要求与当前实例相同：Clean all 后保留在 runs/exports 的包，可在重新 Prepare/Init 建立兼容环境后继续用同一 Verify 命令核验。核验不恢复或接管旧运行，不改写包、原任务及旧运行记录，仅保存当前受管日志/租约；来源 ID 和内容哈希不是制作者认证，也不产生清理权限。

包明确 local_only=true、redacted=false、share_ready=false：原消息、源码和日志仍可能含私密内容，名称守卫不能证明没有秘密，不能直接上传或公开。导出器只声明自己的模块/schema 来源，不补写旧会话未知的 Git/lock/SDK 执行版本；应用结果仍未知。Export 不删除源、不自动清理或解锁整树清理：非空终态 work，以及 tmp/cache 中检测到的任务快照或未发布导出暂存，仍须在具体 Clean 预览和 Apply 请求中明确 -DiscardEvidence；单个矩阵包不覆盖所有管理记录，all 也不因已导出而自动放行。普通清理保留 runs/exports。

程序接口为 preview_export / save_export_plan / apply_export / verify_export；参数与反例见 exporting.py、tests/test_exporting.py、tests/test_export_cli.py 和 tests/test_cleanup.py。对象约束见 [模块规格第 1.3.4 节](../../docs/design-contracts.md#134-停止记录的本机私有证据导出)，资源和清理协议见 [开发设计第 9 节](../../docs/development/system-design-draft.md#9-本机环境资源限制与集中清理)。

合成输入自检位于 `tests/test_offline_evidence.py`，显式案例身份与哈希拒绝反例位于 `tests/test_case_identity.py`；领域对象和候选目录反例位于 `tests/test_contracts.py`、`tests/test_workspace.py`，固定响应闭环、HTTP 模拟协议、策略分支、接受契约、模型请求账本、逐工具授权/恢复、因果记录和修复决策交接分别位于 `tests/test_agent_loop.py`、`tests/test_remote_contract.py`、`tests/test_policies.py`、`tests/test_action_acceptance.py`、`tests/test_model_calls.py`、`tests/test_tool_authorization.py`、`tests/test_causal_diagnosis.py`、`tests/test_decisions.py`。这些检查不读取或运行 data_B 中的应用，不调用真实模型或设备。当前设计与待实现功能见 [技术设计](../../docs/development/system-design-draft.md) 和 [模块契约](../../docs/design-contracts.md)。

模型账本的程序接口是 `ModelCallIntent`、`ModelCallStore` 和 `invoke_accounted`：调用前持久登记并占用次数，发出后若丢失响应则保留未知状态，同一 ID 不重发；收到的 `AIMessage` 保存为可恢复响应，格式错误与 Token usage 分开记账。records 按登记顺序、任务/运行和可选用途只读列出记录；usage_summary 始终计算全部用途。固定响应模型根据实际传给 SDK 的消息、工具定义和参数生成哈希；模拟请求模型以 PreparedRequest 的唯一规范化发送体固定消息/工具/参数、整个 body 和 profile 指纹，发送前再次核对。原始 usage 保留，合格计数另行归一化；格式错误的整个响应不执行工具，已知 Token 超限阻止下一次调用而非承诺实际硬限额。完成响应在 Graph 未落检查点时可回放；请求已发出但无响应时报告 model_result_unknown 并停止。软件验收仅使用 MockTransport；CloudChatCompletionsAdapter 的真实调用仍由显式网络许可和实验开关控制。真实提供方兼容性、摘要忠实度和真实计费仍须验证。

两种离线模型共享 context_profile：关闭 SDK 自动摘要和人类消息/工具结果卸载，避免它们写入冻结原版或绕过调用恢复。默认不压缩，序列化消息超限以 context_limit 停止，格式不支持以 context_format_unsupported 停止；字节限额定义在 models.py，与云端 Token 窗口不同。可选主机压缩由 context_compaction.py 的 CompactionPolicy 配置，保留原 Graph 全历史、system、启动任务和近期完整工具组，只替换模型发送视图中的较早闭合前缀。首版重新摘要全部前缀；摘要输入、输出或必须保留的尾部过大仍停止，不支持无限历史。

脚本顶层可添加 `"context_compaction":{"policy":{"enabled":true}}`；默认参数及约束以 CompactionPolicy 为准。固定响应 schema 1/3 还须按实际摘要顺序提供独立 `responses`，每项 `{"text":"归纳文字","usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15}}`；缺失列表在需要新摘要时停止。mock schema 2/4 禁止独立列表，摘要响应与 solver 响应在 model.responses 中按所有实际请求顺序交错。程序入口对应 `compaction_policy=CompactionPolicy(...)`，固定入口额外接受 compaction_responses；策略及提示指纹在 session 10–12 冻结，恢复不得替换。此配置仍不允许真实模型。

摘要使用无工具请求、独立 compaction-model-N 身份及统一预算，work/compactions 保存冻结原消息、有效摘要视图和 solver 绑定。摘要先保存 usage，再验证非空 text_only、输出限额和确实缩短；返回工具或无效归纳以 context_summary_invalid 停止，不重试、不执行摘要工具。结果未落 Graph 前恢复只回放已完成摘要，已发出未保存不重发；摘要耗尽额度后没有免费 solver。摘要包装的 authority 始终是 model_inference，“已通过”不能生成检查事实；reporting.read_completion 同时重建核验来源、响应及有效发送视图。合成验收用 test_context_compaction.py 与 test_compaction_loop.py，归纳质量和模型效果仍待实测。

有限条件使用顺序是：登记含 RuntimeConstraint 的诊断，查看主机输入 ID 和范围，调用 evaluate_condition，再按结果 ID 回读或提交修复决策。工具接收 request_json 和可选 observation_id，不接受模型自填的值或结果；schema 可从 inspect_analysis(include_schema=True) 获取。ConditionRequest 引用诊断的假设和绑定，声明有限算子及完整范围。当前支持 array_index、data_view_access、typed_array_view 和 ready_at_use；不求值诊断表达式，不自动提取源码值或采集真实运行值。缺值、单位/范围/前提不符返回 unknown，非法目标或诊断引用拒绝。

主机合成测试可先建立 SyntheticObservations，再通过 `ConditionStore(workspace, registry).evaluate(request, observation_id)` 或 `run_fixed_agent(..., observations=registry)` 接入。registry 只由测试代码提供，保存为 condition-inputs.json；CLI 暂不开放注入文件参数，默认空 registry。省略 observation_id 得到 missing_observations 的 unknown，未登记 ID 拒绝。inspect_analysis 默认列出主机 fixture ID/范围和 condition_result_ids，传 condition_result_id 可回读完整记录。原有 ConditionEvaluator 的不保存接口仍可使用。

conditions/<result_id>.json 的 ConditionRecord 绑定结果内容、输入 registry 指纹及诊断包哈希；相同输入回读原记录。回读会重新核对源码/候选实际字节、诊断、输入和完整计算，拒绝改写结果、换输入或提高来源层级。恢复不重复计算登记动作；完整性核对仍会重算有限谓词。结果的 satisfied/violated 只表示固定合成输入的计算，applicability_authority 为 model_inference，根因确认保持 unknown、应用验证保持 not_run，不生成 CheckLedger 结果。

DecisionRequest 可增加 condition_result_ids 与完整 condition_scope，主机要求结果来自同诊断、同假设方向和完全相同范围。开启 obligation_tracking 后，violated 只产生 condition_review 的相关方向重审建议；satisfied/unknown 或关闭关联不确认修复、不消除未知，也不硬拒绝义务。基础/增强共享同一计算工具和总预算。详细前提见 [模块规格第 3.0 节](../../docs/design-contracts.md#30-首批有限条件检查)。边界、保存和交接分别自检 tests/test_conditions.py、tests/test_condition_store.py、tests/test_condition_handoff.py，均通过受管 Run -Task test -TestPath 执行，不运行 data_B 应用。

`value_sources.py` 提供独立的程序接口，不是 CLI 或 SDK 工具。由主机代码建立固定 profile 的 SyntheticValueAdapter，并提供 SyntheticBindingFixture；`ValueSourceStore(workspace, adapter).bind_operation(proposal, fixture_id)` 核对诊断、冻结版本和 UTF-8 原字节锚点。adapter.add_capture 追加固定 SyntheticCaptureFixture，`capture_values(request, fixture_id)` 先保存请求，再登记合成凭据；binding/intent/receipt/receipts 回读只做核验，不新建记录。工作目录内 value-sources/ 保存 manifest、bindings、requests 和 receipts；重开继续提供可信 adapter 与所需历史 fixture，不能只凭磁盘标签信任来源。请求不得自带值、角色映射、观测事件或确认状态。

这里的 source_literal/execution_sample 是模拟请求模式，输出来源始终 synthetic_fixture；没有真实语法/类型适配、literal 提取、设备动作或新条件结果，也不改变 ConditionStore 和 session schema。测试覆盖原字节/父操作、参数角色、对象实例/代次/时序、缺前提、不可改写和恢复反例，执行入口为 `.\scripts\local.ps1 Run -Task test -TestPath tests/test_value_sources.py`。完整职责及未来迁移见 [模块规格第 3.0.1 节](../../docs/design-contracts.md#301-可信采值与算子绑定)。

`knowledge.py` 提供独立主机存储接口，没有 CLI、SDK 工具或自动学习。主机先审查完整 `SolverProjection` 文档和来源用途，准备 `HostMaterialManifest`；`HostMaterialRegistry(source_root, manifest)` 在只读构造时捕获批准原字节。然后在预建的受管目录中建立 `KnowledgeStore(root)`，以只含材料 ID 的 `ExperienceDraft` 调用 `admit_entry`，以明确条目和 `PermissionScope` 调用 `freeze_snapshot`。`read_entry`、`read_snapshot`、`read_projection(snapshot_sha256, entry_id)` 核验并回读具体内容，不新建文件、不读取最新别名、不补缺失材料。后续 SDK 调用还须绑定真实任务范围及主机选定快照。

store 内 `materials/` 保存原批准字节，`entries/` 保存内容及来源权限，`snapshots/` 绑定精确条目、投影、权限与检索声明；快照的 skill_files 字段保持空，正式通用过程由 resources/skills JSON 经批准知识和 static_skills 编译接入，不在该字段另建资源清单。单记录、批准来源总量和一次引用集合的上限由 `knowledge_contracts.py` 定义，超限不截断；叶文件、祖先、读取时变化及闭合收尾均核验，采用串行主机读写协议。历史只接受主机合成描述，报告/产物哈希不等于实际报告认证，应用资格恒为无；历史范围和未知/失败会保留在指导中，原任务/候选/oracle 描述仅供审计。哈希不能判断自然语言是否含答案，完整指导须主机审核。原来源更新需新 registry，新条目或快照不会改写旧内容。字段、准入及未来协议迁移见 [规格第 3.1.2 节](../../docs/design-contracts.md#312-经验检索和适用性交接)。合成反例通过 `.\scripts\local.ps1 Run -Task test -TestPath tests/test_knowledge_contracts.py` 与相同入口的 `tests/test_knowledge_store.py` 执行。

检索入口为 `KnowledgeRetriever(store)`：主机先 `build_index(snapshot_sha256, scope)`，再提供 `MemoryQuery(snapshot_sha256=..., scope=..., tags=(...), text=..., offset=..., limit=...)`。`query(query)` 返回规范 `RetrievalRecord` 且不写文件；需要保留时用 `record(query)` 显式发布。`read_record(record_id)` 从原快照和索引重算全文；`read_projection(record_id, entry_id)` 只返回该查询页实际命中的完整指导。它们核验保存查询的范围，尚不认证调用者的真实任务权限。

`indexes/` 的每快照 SQLite 与描述文件只保存批准指导的派生字段，不索引原历史、成本或对象 ID；`retrievals/` 保存显式发布的查询记录。标签规范后去重匹配，查询原拼写保留并拒绝规范重复；文本以去重 Unicode 词作子串匹配，空查询可按 ID 浏览。分数使用冻结权重，零分和未知标签条目保留，相关性不等于适用性。每次检索核完整来源及索引全部行，再按固定参数 SQL 排序；缺失、破损、附属日志、结构/行错配、超量或超数值能力均拒绝，不自动重建。独立容量和每连接 SQL 门由 `retrieval_contracts.py` 定义；query 的索引核验和排序分别用受限连接，累计读取包含闭包收尾。合成验收入口为 `.\scripts\local.ps1 Run -Task test -TestPath tests/test_knowledge_retrieval.py` 及同入口的 `tests/test_retrieval_contracts.py`；SDK 接入使用下面的主机边界。

普通经验检索已接实际 SDK 程序接口：主机将审核过的 `KnowledgeRetriever` 与精确 `TaskKnowledgeBinding` 包成 `HostKnowledgeInput(retriever, approval)`，首次 `run_fixed_agent` 或 `run_provider_agent` 传 `knowledge=host_input`。入口把该快照/索引的完整原件闭包复制进当前 work 的 `knowledge/`，固定 `knowledge-profile.json` 和批准记录；不复制整库、外部旧查询或未批准内容。模型获得 `search_experience(tags, text, offset, limit)` 与 `read_experience(query_id, entry_id)`，不接收快照、scope 或文件路径参数。搜索返回完整命中页，回读只允许该页实际条目，保留全部前提、未知问题、步骤、保留行为及反例。无批准时原 14 工具及 10/7/4 形状不变，有批准时增加两工具；复用既有能力和调用/工具/Token/时间预算。

知识续跑只传 `resume=True, knowledge_approval=original_approval`，不得再次传入库；主机必须明确提供原批准，不能从已存 profile 自行取得授权。原外部库不可用仍可从 run 原件恢复。来源、全部查询和完整 ToolMessage 回执在可写账本及 Graph 前只读审计；完整运行缺账本拒绝，未保存结果保留未知，已完成搜索/回读回放而不重做。搜索发布前既核原件容量，也核报告中两处完整知识片段的 ASCII 转义容量及共享工具预算下的关联余量，拒绝后不写新查询、不截断指导。报告关联原 solver 请求、动作身份、批准范围和完整原指导，孤立查询不冒充模型使用。私有 Export 保留这些原件、批准、查询及索引，可在迁移后只依赖包内任务/知识重聚合。固定/模拟脚本 schema 5/6 已通过完整外部批准接入受管 repair/resume，启动消费明确库及原件，恢复只用 run 内原件和原批准；程序接口也须在受管任务内调用，不能直接绕过 Job。相关测试为 `test_knowledge_run.py`、`test_knowledge_sdk.py`、`test_knowledge_reporting.py` 及受管知识配置反例。当前无真实模型或应用实验；三态发送另走下述显式 SDK 策略入口，学习收益仍未知。

经验前提交接入口为 `ApplicabilityStore(workspace, task_directory, ledger, retriever, approval)`。`approval` 是主机显式提供的 `TaskKnowledgeBinding`，固定实际任务/运行、分析版本、日志、批准知识快照与 scope；构造不创建目录，`bind()` 才固定该 run 批准。`record_query(tags=..., text=..., offset=..., limit=...)` 由主机填入快照和范围，生成绑定查询；`prepare_context(bound_query_id, focus)` 冻结当前诊断、假设、候选和已核验原材料。模型提案只使用该 context 的经验页和登记引用，`review(proposal)` 保存完整前提判断、指导、原未知问题、当前义务及三态建议。这个独立 Store 本身不调度 CLI、SDK、模型或执行；实际 SDK 集成由下述 applicability_handoff 入口承接。

run 内 `experience-bindings/approved.json` 保存主机批准，`experience-queries/` 保存绑定查询，`applicability-contexts/`、`applicability-materials/` 保存冻结上下文及原字节，`applicability-reviews/` 保存审查记录。`read_query`、`read_context`、`read_review` 对照外部批准并只读重核；合法后续检查不重写旧上下文，新证据需新 context。原版源码/日志不能标为候选观察，直接或递归推断中的执行依据须匹配当前候选与版本；历史同异常案例不能代替这些依据。语义状态仍为 model_inference，保存 supported 只提供可尝试步骤，不确认根因、修复或业务成功；contradicted 暂停照搬，unknown 提供原查证问题，不改共同接受门。

完整知识、冻结材料及输出共同受 `applicability_contracts.py` 的容量约束，不同根之间采用保守计数；捕获和审计读取另有累计字节/次数门，闭合收尾也计入。发布前先完成原件关闭核验，冻结上下文还按下一次审计的实际路径预检预算；回读从原诊断材料重验引用、位置和版本，重写哈希不能绕过准入。`prepare_context` 前段复用的当前任务核验仍由外层 Job 保护，尚无完整累计 I/O 计量；不得把这些边界当实际性能或 Token 指标。合成验收入口为 `.\scripts\local.ps1 Run -Task test -TestPath tests/test_applicability_contracts.py` 和同入口的 `tests/test_applicability.py`。SDK 入口显式传入 applicability_policy=ApplicabilityPolicy(enabled=True/False)，两组固定相同三项三态工具、完整知识和总预算；不传该参数保持普通经验运行。session/report/completion 使用 12/9/6，冻结实际 solver-inputs 请求；模型不能指定当前焦点或父调用。准备工具必须已见查询；已见诊断和假设时冻结 context 并提供 review_proposal_schema，否则返回 unknown 与普通指导，审查要求已见完整 context，兄弟工具后产生记录及摘要里的 ID 不取得可见权限。缺焦点回普通指导与 unknown，关闭组只提供普通指导。报告保留完整 context/review 和实际调用来源，恢复先只读核原件及原配置，导出能从包内重核。独立内层对象仍保留其显式主机输入边界，SDK 外层新增 actual_solver_input 认证，语义仍为推断。受管软件自检见 test_solver_visibility.py、test_applicability_handoff.py、test_applicability_sdk.py、test_applicability_provider.py 和 test_applicability_reporting.py；三态与静态策略已通过 schema 5/6 显式知识配置接入固定/模拟脚本及受管 CLI，自动模型学习仍待接入。

`acceptance.py` 当前只供独立程序调用：准备者冻结与候选无关的共同协议，可信打包器绑定完整运行源码，执行端按检查 ID 回传原版和候选的结果，接受门对缺失、模拟、版本错配和行为失败返回 `not_verified`。该独立契约接口不负责本机执行；competition 使用 local_execution.py 的完整工程绑定与四项固定命令，真实环境及业务 oracle 仍须主机接纳。该模块的合成 `verified_under_protocol` 测试值不是本项目已修复应用的成绩，软件通道的实际验收见 STATUS。

`static_skills.py` 从 run 内批准的人工条目生成完整过程及冻结清单，read_run_static_skills 只读重核；Agent 程序入口 run_fixed_agent/run_provider_agent 显式接收 static_skill_policy=StaticSkillPolicy(enabled=..., entry_ids=...)，同时必须提供启动知识和显式 ApplicabilityPolicy。两组注册相同 read_skill，共用原预算；关闭只去目录注入，保留相同全文结果和普通检索，工具描述说明怎样从批准投影摘要取得同一 ID。报告从完整原件和账本区分读取/实际可见性，清单 5 固定共同静态资源，私有导出保留全部正文/回执/实际输入。恢复仅接原 knowledge_approval 及相同策略，从 run 原件核验，无需外部库；可写恢复前核原生缓存，静态 saver 使用严格 serializer。字段与容量见[规格第 3.1.3 节](../../docs/design-contracts.md#313-skill-版本验证与演进)。受管软件自检包括 test_static_skills.py、test_static_skill_handoff.py、test_static_skill_sdk.py、test_static_skill_resume_cache.py 和 test_static_skill_reporting.py，均使用同一 local.ps1 Run -Task test -TestPath 入口。固定/模拟脚本 schema 5/6 与受管 CLI 已消费明确知识批准及静态选择，不要手工向旧 run 加资源。独立版本验证入口见下文，合成可见性不代表修复或学习收益。

`SkillValidationStore(root)` 提供独立主机程序接口，root 必须预建在受管目录，构造与 read_version/read_plan/read_receipt 均不写文件。`create_seed(projection, approval=...)` 消费完整 HostSkillVersionApproval；`create_revision(parent_version_id, feedback_store, candidate_id, approval=..., parent_approval=..., candidate_approval=..., feedback_approval=...)` 消费原父版本及 FeedbackStore 候选/capsule 两份原外部批准。新版本保存完整 host_curated 投影、父链权限和全部声明学习谱系，把原候选与 capsule 闭包复制到 source-feedback/；原反馈库移走仍能读回。版本资格始终 unvalidated/none，学习模型 not_run、learning_tokens=null，不自动入旧知识库、索引或 Agent。
模型来源版本另使用 `HostModelSkillVersionApproval` 和 schema 2。主机审核固定父版本、LearningDraft、完整 LearningSource 和投影；`create_model_revision(parent_version_id, learning_store_or_source_store, draft_id, approval=..., parent_approval=..., learning_approval=...)` 保留 model_inference、原成本前缀、全操作成本及权限/祖先谱系，原学习库移走后仍可回读。人工旧代不接新字段；批准版本仍 unvalidated/none。模型计划与回执同用 schema 2/Manifest6，学习及验证预算轴分别保存，完整祖先学习成本不能只取合法草案的那次调用。版本独立测试见 tests/test_model_skill_versions.py；SDK 版本消费及部署的具体验收范围见 STATUS。

`HostValidationSkillInput` 绑定事前计划及精确案例/角色，用于候选验证；`HostDerivedSkillInput` 绑定已发表冻结选择，用于新部署。`run_fixed_agent` 的 version_skills 是新启动输入；恢复须提供完整原 version_skill_approval，默认沿用原目录开关。它们复用原生 middleware 和 read_skill，不增加运行权限；新会话/报告/完成记录为 14/11/8。原件位于 version-skill-sources/，正文位于 version-skills/，完整祖先来源成本与当前 run 成本分开报告。这些是程序接口，尚无此代受管 CLI 配置；tests/test_model_skill_sdk.py 的实际通过范围见 STATUS，未通过的部署连接不得视为已可用。

`freeze_plan(spec, evidence_root, approval=...)` 在全部预定 work 不存在时固定 PlanSpec、精确 CaseSelection、代表/边界对、旧新角色目标、共同配置和主机原根身份/时间。`record_validation(plan_id, case_id, role, evidence_root, approval=...)` 只读消费计划后的实际停止 SDK 合成 run，保存完整原件及独立重聚合；`read_receipt` 可在原运行移走后只依赖副本重核。当前是同知识/静态资源池的目标过程验证，两个单运行清单 5 保持原含义，不是不同知识快照效果比较；followed 只核完整目标 read_skill 和后续 solver 可见，未读/不可见/串臂仍保留结果和成本，不能直接当因果归因。验证来源固定 candidate_validation、allow_learning=false，应用资格无。每次 API 共用复制/闭合/递归回读预算，版本/计划/回执正式发布前预留完整回读成本；内部聚合 I/O 另有边界。这些接口本身不执行模型/检查/Graph，也未增加学习 CLI。对象与反例见规格第 3.1.3 节；合成测试入口为 `.\scripts\local.ps1 Run -Task test -TestPath tests/test_skill_validation.py` 及相同入口的 tests/test_skill_validation_integration.py，实际结果只见 STATUS。

独立演进入口为 `SkillTransitionStore(validation_store)`，对象在 skill_transition_contracts.py，存储和重算在 skill_evolution.py。主机先将完整 SyntheticValidationPolicy 规范对象写入 PlanSpec.policy_json，再 freeze_plan、执行验证和保存回执；七项覆盖/质量/Token/调用/尝试阈值默认 None，无真实默认，不能在看到结果后补策略。`decide_skill_transition(plan_id, approval=HostTransitionApproval(...), plan_approval=original_plan_approval)` 从明确批准的回执和完整历史矩阵追加 synthetic_pending/rejected/eligible。结果/旧缺角色或选定的 synthetic_tokens/reserved_calls 成本轴未知保留 pending，所有 attempt 的覆盖、损害、成本和分母都计入；同计划补齐不重复计费，跨计划父/策略/共同配置及 task/log/lineage 独立性继续核验。实际金额/耗时与人工学习 not_run/null 单独保持未知，不属于当前合成门，eligible 不表示测得这些费用。`read_transition` 只读重算来源，不补写未知、不自动解释失败或提炼修法。人工缩界/补分支须先形成完整新候选，再经 create_revision 和独立验证。

`publish_alias(approval=HostPublicationApproval(...), version_approval=..., transition_approval=...)` 只追加 synthetic_sandbox 发表：initialize_seed 要求明确批准的原种子；新 activate 要求候选当前判定 head 为 synthetic_eligible，且修订与当前父版本相符；后来追加损害/未知后不能再使用旧合格判定首次激活，历史回读和完全相同的幂等返回仍核原事件；rollback 只能回到此前发表过的当前父链祖先。后续发表须绑定 current previous_publication_id，前驱过期拒绝。`freeze_alias(alias, approval=...)` 返回 FrozenSkillSelection；`read_selection(selection, publication_approval=...)` 核原发表并返回完整原版本投影，后续别名更新不改变旧选择。读操作不创建文件，计划及来源按下面的单操作首尾协议核验，事件发表后核字节/FD，整次 API 共用闭合读取预算；非全 I/O 或峰值内存计量。冻结选择可经独立版本程序入口接入 SDK，须提供完整外部批准；此代尚无受管 repair CLI 配置。人工链保持 none/not_run/null，模型来源使用 schema 2 及完整独立学习成本；费用和耗时无依据仍 unknown，具体验收只记 STATUS。

私有 _EvolutionBudget / _PlanReadView 只在同一 API 预算与完整原批准内复用 Plan 和 receipt 的临时核验结果。每份回执首次完整捕获原件、账本并独立重聚合，保留 payload 与 metadata 的原始字节、FD/路径身份及目录成员证明；内部视图接管最终关闭，避免捕获后额外关闭一遍。成功读取/幂等返回或正式写入前，_close_plans 绕过视图重读全部涉及的计划、父版本和反馈源，并关闭每份回执的原始证明，全部读取仍计入同一预算；修改后恢复字节或 mtime 也不能替换原身份。新部署及激活在关闭后再轻量核对完整链成员和已认证头事件，不重新消费回执语义。finally 丢弃视图并使预算失效，下一次调用重新认证；公共 SkillValidationStore 仍即时关闭，接口未改。这只是减少同操作重复核验，不宣称性能达标或统一全 I/O/峰值内存限制。

瞬时目录闭合只在实际目录上规范化已观察到的 NTFS 内部目录位 0x10000000；卷、文件 ID、类型及其他属性仍保持精确核验，路径 guard 继续拒绝重解析点。真实目录替换、成员/字节漂移仍拒绝。工作上下文的原目录锚及持久 Plan/owner 身份不改，不倒填历史批准；实际失败、反例和整链重跑结果见 STATUS。

来源历史成本通过显式程序入口 `VersionSkillService.research_cost_inventory()` 读取：先认证当前冻结任务/批准及 run 原件，再由 `research_accounting.py` 提取完整学习调用、事前验证矩阵/原行成本和发表/退回历史。相同调用或验证 run 跨来源只计一次，冲突原件拒绝；缺少预定角色或 usage 保持 unknown，人工学习为 not_run/null，退回种子也不抹掉模型子版本历史成本。`union_research_cost_inventories` 可只读合并多个完整清单；清单一致性校验不能替代外部批准和原闭包认证。该入口不调用模型、不写文件、不改变旧 session/report/清单或共享预算；部署实际成本、全流程计时与公平比较仍待接入。测试和实际结果见 DEVELOPMENT/STATUS。

`backends.py` 和 `remote_service.py` 另提供 HTTPX 客户端及 FastAPI 模拟服务的程序接口。`tests/test_remote_contract.py` 使用进程内 `TestClient`，显式推进模拟工作者状态，检查请求幂等、响应丢失后的恢复、取消待确认、产物哈希及离线行为；不启动常驻服务、真实设备或应用构建。模拟服务只返回 `scope=synthetic` 和 `application_verification=not_run`。当前本机执行软件通道由 local_execution.py 和 competition 承担；真实 HarmonyOS 工程、SDK、设备及业务协议尚未接纳和验证，不能把 HTTP 协议或 mock_demo 自检当作应用验证。
