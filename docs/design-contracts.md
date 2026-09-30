# 模块契约与规则规格

状态：当前规格草案，部分契约已有隔离合成实现。最后维护：2026-09-30。
职责：维护对象不变量、控制器分支与规则卡。配套 [总体技术设计](development/system-design-draft.md)。因果诊断、修复义务、修复决策与候选的结构化交接、模型账本、逐工具授权和动作恢复已接入固定响应离线循环；应用接受门、模拟 HTTP 协议及策略分支也已有合成实现。因果关系的实际检验、运行条件计算、真实模型、可信业务判据和本机应用执行仍待实现。文中案例推演属于分析人员材料，不输入未来的独立评测求解器。

## 1. 数据契约与不可破坏的条件

离线原型与首批领域对象分别校验，不能把 evidence_ready 报告直接当作 Diagnosis。领域记录 schema_version=1 已包含下述因果字段；有些可空，以容纳尚未查明的内容。诊断提交本身不产生根因确认；应用打包与验收对象仍有独立契约。

### 1.1 版本与来源

所有记录包含 schema、task_id、run_id、record_id 和 created_at。所有涉及源码的记录绑定 source_snapshot_sha256；分析源码和完整运行源码分别使用 analysis_snapshot_sha256、runtime_snapshot_sha256，不要求二者相等。二者之间通过文件清单、共享业务源码哈希及打包记录建立关联。

SourceLocation 包含 snapshot_sha256、相对 path、1 起始的 start_line/end_line 和可空 symbol；修改前与修改后的坐标必须分开。禁止使用单一的 confidence 浮点数替代证据。

Evidence 至少包含：

- kind：log_observation / source_fact / model_inference / local_execution / application_execution。
- artifact_sha256，以及适用的日志行、源码位置或 execution_id。
- statement、直接引用的依据 evidence_ids、derivation。
- source_snapshot_sha256；无关源码的纯日志项可空，但日志必须绑定任务输入。候选执行证据另存 evaluated_candidate_id、execution_snapshot_sha256、execution_scope，分析基准不能冒充实际执行版本。

引用检查器只负责存在性和版本一致性；模型推断不会因为引用合法就变成运行观察。运行产物须由执行适配器登记，不能接受模型自行填入 application_execution。

### 1.2 诊断对象

Diagnosis 保存 exception_locations、hypotheses、selected_hypothesis_id、unknowns 及可空 feedback_candidate_id。异常位置与根因位置分别填写；未知根因可以暂空。每个 Hypothesis 包含：

| 字段 | 内容及约束 |
|---|---|
| root_cause_locations | 原始源码位置列表；允许暂空，不用异常位置填满未知根因 |
| constraint | RuntimeConstraint：family、operation 源码位置、bindings、condition、semantic_source_evidence_ids；绑定含 binding_id、expression、role、可空位置和 evidence_ids |
| causal_steps | CausalStep：step_id、from_binding_id、to_binding_id、relation、authority 和 evidence_ids；端点须属于本假设的绑定，模型关系保留 model_inference |
| support_status | supported / contradicted / unknown；这是证据判断，不是正确率概率 |
| support_evidence_ids / contradicting_evidence_ids | 支持和反驳分开且不重叠；约束、步骤与绑定也须引用已登记依据 |
| obligation_ids | 与同一诊断包的修复义务双向关联，引用不能缺失或互相矛盾 |
| missing_evidence | 哪个事实未知，以及为何可能改变修改方向 |
| distinguishing_check | 可空 DiagnosticQuestion：question、至少区分两个已登记假设的 observation_routes 及 retain_unknown；这里只记录问题，尚不执行检查或决定修法 |

`diagnostics.py` 已将上述对象接入固定响应工具。`inspect_analysis(include_schema=True)` 按需提供主机任务身份、DiagnosisSubmission 的 JSON schema 和接纳规则；按 diagnosis_id 可回读原记录。`record_diagnosis(diagnosis_json=...)` 接收诊断、源码/日志引用、模型推断、已有证据 ID、失败检查引用及义务；旧的 path/line/reason 简单路径仍保留，不能和结构化路径混用。

源码文本由主机按版本和行范围读取，日志核对原字节哈希后读取，模型只能提交推断和引用，不能自填执行事实。执行证据只从已登记候选的已保存失败检查产生，核对候选源码哈希与 execution_id；恢复引用旧执行依据也须明确相同 feedback_candidate_id。所有模型提交的假设保持 unknown、关系保持 model_inference；这不是禁止后续可信检查确认它们，而是防止模型自授确认。引用闭包不得缺项或循环，已有证据 ID 不得改写。诊断、证据和关联义务在同一个不可改写的文件中原子保存；新假设或修订使用新 ID。程序只检查结构、引用和来源，不能证明因果解释正确，也未自动计算运行条件。

### 1.2.1 修复义务和动作提案

RepairObligation 用 record_id 作为 obligation_id，包含 kind（runtime_condition/business_behavior）、statement、bindings、source_evidence_ids、hypothesis_ids、authority（verified_requirement/interface_rule/existing_behavior/model_inference）、checkability（executable/manual/unknown）、status、scope 和共同任务/版本身份。模型提交只接纳 model_inference/unknown；checkability 只是拟议检查方式，不证明检查已实现。Hypothesis 与义务双向关联；CandidatePatch 关联 restores 与 preserves。结构化义务随诊断包保存，简单 record_obligation 保存相同规范的独立记录；策略辅助对象 ObligationClaim 不作为新的文件格式。当前合成反馈只能关联候选已经声明的义务，关联解释保持模型推断；真实 Check/Result 与可信义务检查的接入仍待实现。

模型推断的义务只能作为假设，不得仅因某候选违反该推断就机械拒绝它。局部执行只能支持对应输入与路径，不自动证明全局成立。可信语义不足时明确 unknown。

`ActionProposal` 是模型意图，不是执行结果。对象绑定 task/run/分析源码版本，保存 action、target_ids、evidence_ids、candidate_id、reason、required_capability、observation_routes、unknown_route 和 estimated_cost_basis。`record_analysis` 单独表示写入诊断、义务或反馈记录，不冒充观察或源码编辑。诊断探针至少区分两个修复方向，观测映射须能改变方向，并保留“未命中预测”的 `retain_unknown` 分支。`authorize_action` 只用主机持有的任务版本、已登记引用、候选、能力和剩余预算给出允许/拒绝；模型自报的能力与成本不构成授权。`finish` 还须引用已通过独立接受门的候选。

固定响应 SDK 的同步工具调用已通过 `ToolAuthorizationMiddleware`：主机把工具名与参数映射成动作，校验参数、版本、已登记引用、候选和实际能力；可选 `proposal_json` 只能补充模型的理由与引用，不能改变工具对应的动作/能力或候选，工具实际引用也不能被省略。主机根据已记账的模型响应生成稳定 action_id；模型响应内的 call ID 元数据由适配器覆盖，不接受模型自授身份。已付费且保存的当前响应可在没有下一次模型调用额度时完成合法工具动作，但仍受独立工具调用预算、候选预算和截止时间限制。实际探针、停止请求、资源租约及应用结果归类仍待真实控制器集成。

`ToolActionStore` 在工具产生效果前记录授权与 `dispatched`，工具返回后保存完整 `ToolMessage` 及哈希再交给 Graph。`completed` 仅表示工具结果已保存，授权通过也不表示工具执行成功。相同模型调用与工具调用身份、相同参数的完成结果恢复时只回放；身份对应的参数改变则拒绝。已派发但无结果记录时保留未知，停止为 `action_result_unknown`，不盲目再次编辑。拒绝本身只有工具错误消息，不生成源码修改、候选或检查事实。原始工具结果只留受管 SQLite，报告只列授权、状态和结果哈希；这不是应用成功证据。

反例 Counterexample 保存实际输入或产物引用、执行版本、观察、violated_obligation_ids 及 affected_hypothesis_ids。关联本身也可能是模型推断，必须标记；只反驳观测所覆盖的主张。重试时优先携带有效反例与未知项，不重复携带所有长日志。

### 1.2.2 模型请求、响应与恢复

`ModelCallIntent` 是主机生成的稳定调用身份：call/task/run ID、调用用途（求解、格式修复、上下文压缩或 SDK 重试）、提供方与具体模型 ID，以及完整有序消息、工具 schema、生成参数的规范化哈希和允许的工具名。哈希对应的原始请求必须由可信适配器核对；仅填入任意哈希不能证明实际发送内容。调用次数从登记时占用，同一 task/run 内所有用途共用次数预算，不能把格式修复或压缩隐藏在 SDK 内部。跨 run 的总预算尚未定义。

`ModelCallStore` 用项目受管目录内的 SQLite 保存 `reserved → dispatched → completed` 或 `dispatched → unknown`。先持久化意图，紧邻外部调用前标记 dispatched；重启后已完成的同一请求回放已保存的 `AIMessage`，不再发请求。已发出但无完整本地响应时，同一 call ID 禁止重发，结果与费用维持 unknown；确需另试须显式建新 call ID 并另占次数。标记 dispatched 与网络实际送达之间的崩溃窗口也保守算 unknown。提供方通常不保证以 call ID 查询或幂等，不能承诺 exactly-once。异常捕获后的原因只记录类别，不把异常文本中的凭据写进账本。

收到响应后保存规范化消息及哈希、提供方消息 ID（如可得）、工具调用名称/ID/参数摘要、格式问题与 `usage_metadata` 的真实 input/output/total。原始消息仅保留在本机受管状态用于检查点落盘前恢复，不进入 Git 或默认汇总导出。格式错误也算已完成的一次调用，不能丢掉它的 usage；缺失或无效 usage 单独标记 `missing`/`invalid`，统计只有已知 Token 和未知调用数，不从文本长度补估。`text_only`、模型自称成功、工具参数或模型推断都不产生执行证据。固定响应 Deep Agents 模型入口已通过账本并把主机调用身份绑定到工具授权；Graph 检查点之前的完成响应可回放，已发出且未保存的响应停止为 unknown。真实 LangChain 提供方、其底层重试的逐次计费、实际 Token 硬限额和应用接受门接入仍待完成。

### 1.2.3 已登记诊断到修复决策

`plan_repair_action(plan_json=...)` 接收 `DecisionRequest`，由 `inspect_analysis(include_schema=True)` 提供 schema。请求绑定 task_id、run_id、diagnosis_id 和含源码版本的 PolicyInput；失败后再决策须成对传 candidate_id 与已保存的 feedback_id，不接受模型自填失败观察。旧的独立 PolicyInput 仍可供纯策略函数使用，已不作为 SDK 工具输入。

每个 RepairDirection 包含 direction_id、hypothesis_ids、support_evidence_ids、obligation_ids、repair_key 和 acceptance_key。方向须引用本诊断的假设和证据，包含相关假设已关联的义务；义务须与方向假设或同包依据有关。检查的 outcome→方向映射须对应已登记 DiagnosticQuestion 的 outcome→假设映射；类型和能力须匹配。这里只核对引用，不能证明证据足以支持方向。repair_key/acceptance_key 相等是模型声明的修法/验收等价，未通过补丁或业务行为验证。

主机覆盖模型填写的可用能力和探针预算：离线路径仅保留主机实际提供的 read_source/read_log，执行探针预算为零。同修法且同验收直接建议候选；不同修法须有区分力、能力和预算才建议补查；否则允许带 unknown 尝试或 unresolved。建议本身不读取源码或执行探针，实际读改查仍经过共享工具授权与预算。

有效反馈必须来自同任务已冻结候选的已保存失败合成检查，匹配候选源码哈希、execution_id 和原观察。引用另一诊断时该诊断须明确 feedback_candidate_id。失败关联的义务必须已由原候选声明；scope=synthetic、evidence_kind=local_execution、link_authority=model_inference。只重新考虑相关方向，不硬拒绝推断义务，不把失败解释升级为已证实的因果关系；关闭增强时保持一般未知路径。

`decisions.py` 保存不可改写的 DecisionRecord：规范化请求、诊断/义务/反馈上下文哈希、策略开关、原始策略建议、反馈建议、最终 next_action 及推断层级。身份摘要排除接纳时间等审计元数据，但保留语义输入；相同请求和上下文回读原结果。inspect_analysis 可列出决策/反馈 ID 并按 decision_id 回读；报告包含这些决策。保存决策属于 record_analysis，不冒充执行检查。

候选可选成对引用 decision_id/direction_id；主机核对同诊断、已提议方向和候选声明义务。决策是建议，允许模型选择其他已提议方向；未知建议本身不阻止合法候选登记。当前还没有运行条件计算、自动确认根因或真实应用反馈接入。

### 1.3 补丁、验证和尝试

CandidatePatch 当前保存 candidate_id、ordinal、parent_snapshot_sha256、candidate_snapshot_sha256、diff_sha256、diagnosis_id、changed_paths、restores、preserves 及可选成对的 decision_id/direction_id。拟议 intended_constraint、preserved_behaviors、verification_obligations 的信息目前由关联诊断/义务表达，尚未独立实现这些字段。完整 diff 登记后才进入任何候选专属检查；格式错误、无法应用和局部检查失败同样记为已产生候选的失败。

没有生成完整候选的模型失败记录为 generation_failure，计入总成本，但不伪造一个可验证候选。首次候选通过率同时报告“全部任务”为分母的端到端口径，避免把未生成补丁的任务排除后抬高成绩。

W04 的候选工作区从已校验分析快照建立独立草稿；精确文本编辑在临时副本完成后切换，失败不改原草稿。登记时冻结完整 diff、候选源码副本和哈希；仅登记且复核未变的候选可交给后续检查。修订时可以读取前次 diff 和反馈，但新 diff 表达相对该基准的完整修改。该工作区尚不是可构建的运行工程；跨候选诊断中的旧执行证据保留历史版本，只能说明对应旧版本。

W05 的合成执行器只对冻结候选做固定文本检查；先写入含候选/检查/协议 ID 的意图，重启后用同一幂等键重试或读取已有结果。本机真实工具链作业适配器尚未接入，模拟 HTTP 服务的幂等性不能代替设备端核验。`offline_checked` 不是 VerificationResult 中的应用 pass，也不能满足最终共同协议；`application_verification` 始终为 `not_run`。模型工具保留哈希校验日志读取、通用源码浏览、精确编辑和候选登记，关闭 SDK 的任意写文件、shell 和委派工具，以免绕过登记门。

`AcceptanceProtocol` 是候选无关的共同验收输入：task_id、应用谱系组、故障变体、原始崩溃日志和目标故障签名哈希、原始分析与完整运行源码哈希、环境/初始状态、检查列表和准备者身份。故障签名应由准备者按预先固定的 Error 类型、关键消息和目标业务栈规则从原始日志得到，不能在看到候选结果后换匹配口径。候选提交前固定协议规范化哈希；每个 `ProtocolCheck` 指明 check_id、层次（build/target_replay/behavior/regression）、场景、oracle_origin、依据哈希、可见范围、所需能力、超时、是否必要及预定重复次数。正式必要检查不得使用模型临时推断或构造测试作为唯一 oracle；没有独立依据哈希时不能标成必要验收。

`CandidateRuntimeBinding` 把 W04 冻结候选的父分析快照、候选分析快照和完整 diff 重新计算到 candidate_id，再由可信打包器关联共享业务源码清单、打包清单和完整运行源码哈希。仅有两个哈希字符串并不证明分析源码与设备产物一致；真实打包器还须实查文件清单、实际产物及签名。当前只有对象和入口校验，没有真实打包证明。

`VerificationVerdict` 由执行适配器登记，包含 check_id、协议哈希、original/candidate 角色、运行源码哈希、候选 ID、环境/初始状态、重复序号、status、failure_kind、场景是否到达、执行 ID、产物哈希、耗时和 `scope`。`admit_verdict` 不接受模型作为执行证据来源。模型临时自测和候选专属诊断探针可帮助筛选，但不得替代共同验收；故障构造测试中要求错误表达式存在的断言只属构造材料。

`evaluate_acceptance` 当前只做隔离的保守接受判定：必要的四类检查都存在；原始版本在目标场景实际触达，观察到与冻结签名匹配的目标故障，原始版本的构建/正常行为/回归场景通过；同一协议下候选版本的构建、目标回放、业务和回归全部通过；每个预定重复都有 `scope=application` 的执行证据，task、运行源码、候选、环境、初始状态和协议哈希一致。缺项、重复记录、版本错配、场景未到达、故障签名不符、模拟结果、阻塞或不确定结果均返回 `not_verified` 及原因，不补写 pass。这一 gate 只校验可信适配器送来的记录，不能通过合成记录证明设备行为。案例尚无独立业务 oracle 时，协议不满足四层覆盖，不能宣称完整应用修复。

### 1.3.1 案例接纳与协议冻结

案例身份不能只用编号或应用目录名。准备者须明确任务 ID、原材料案例键、应用谱系组、故障变体、唯一选定日志成员及其原字节哈希、分析源码快照、完整运行源码快照、目标操作入口与环境。`CaseCatalog` schema 2 已要求前五项并可绑定场景脚本哈希；拒绝重复任务、案例键或同字节日志。`prepare` 只核验所选文件并生成 analysis_source_v1 快照，在 task schema 2 中留下案例选择摘要，不把脚本复制给 Agent。目录字段和摘要并不证明日志与实际运行源码同版；完整工程、操作和 oracle 仍由评测准备者单独核对。先只读核对日志与源码/运行版本，再建立候选无关的共同协议；同一谱系的变体作为相关样本划分。目录、报告或 README 的“完整”说法只产生候选，不自动取得可评测资格；已知歧义及 021/022 待接入观察见 [数据核查](preparation/data-b-assessment.md)。

准备流程依次是：分析输入可核对、原始完整工程可打包并绑定、目标动作能触发指定故障、正常/边界/回归行为有独立依据。缺少后两项可以继续离线诊断，但不进入正式应用成功分母的“可执行接受”状态；若评测任务清单已冻结，未就绪任务仍作为 blocked 披露，不因某组失败而临时剔除。正式协议和重复次数在观察候选成绩前冻结，所有比较组共用其哈希。模型提出的候选专属检查另记为诊断探针，不能改写共同协议。

oracle 的实际内容与 hash 由独立准备者管理：已核验需求/接口规则、原版正常行为或独立测试可提供依据；故障构造断言、参考补丁和注入答案只属于评测/分析侧，不能混入求解器输入。`visibility=evaluator_only` 的测试细节不发给 Agent；可公开的场景操作仍由统一适配器提供。测试未覆盖的业务语义标 unknown，不让“没有新崩溃”代替功能正确。

W06 的 `ExecutionRequest` 先绑定 task、candidate_id、完整运行源码哈希、补丁哈希、环境、共同协议、初始状态、执行层、重复序号和超时。`run_id + 请求规范摘要` 生成幂等键；重复序号改变表示一次新的逻辑回放。客户端提交前保存意图，响应丢失时只能以同一键对账；服务端 SQLite 对该键设唯一约束，查询不能新建作业。`cancel_requested` 不是 `canceled`，服务重启后无法确认仍在运行的模拟作业标记 `inconclusive`。产物引用绑定 job、内容哈希与大小，服务和客户端分别校验。模拟结果显式 `scope=synthetic`、`application_verification=not_run`，不能转为正式应用 pass。当前 W06 请求尚无 check_id 与场景绑定，真实适配时须在兼容性版本中补齐，逐项产生上述 Verdict；不能按执行层次猜测属于哪个检查。真实执行进程、设备状态确认与应用级结果仍待接入。

Attempt 保存诊断/候选/验证引用、下一步动作及理由、累计和本次成本。FinalReport 保存第一次提交验证前的根因预测与最终预测，不能用成功补丁落点覆盖初始定位成绩。

## 2. 控制器转换规格

控制器只依据可校验记录转换状态。先验证原始版本能按目标协议触发故障；复现入口尚缺失时，允许继续离线诊断，但运行修复任务不能成功终止。

| 当前事件 | 必须保留的信息 | 允许动作 | 禁止推断 |
|---|---|---|---|
| 原始日志映射有歧义 | 全部候选路径、版本未知 | 补查源码和打包来源，或保留未知 | 随机选第一个文件当成精确定位 |
| 两个诊断解释会导向不同补丁 | 区分二者所需事实 | 预算内补查或执行一个探针 | 多做一次相同检查算获得新证据 |
| diff 无法应用或引入编译错误 | 原候选、错误与成本 | 修订生成；必要时更新上下文 | 抹去本次首次失败 |
| 依赖下载/SDK/签名失败 | 环境标识和原始执行错误 | 有界重试环境；否则 blocked | 直接否定业务诊断 |
| 原故障在正确版本继续出现 | 回放确实触达目标的证据 | 检查修复覆盖，或换根因假设 | 仅因“日志相同”认定所有诊断错误 |
| 新行为错误或回归 | 违反的业务义务和反例输入 | 从基准建立新候选，加入反例 | 删除断言或跳过原功能 |
| 全部必要检查通过 | 基准失败与补丁通过证据，版本链 | verified_under_protocol | 从“无崩溃日志”推断所有功能正确 |
| 缺环境/协议，或证据不足 | 未运行层、可恢复条件 | blocked / unresolved | 将未运行层视为默认通过 |
| 预算耗尽或没有新动作 | 所有历史尝试和剩余未知 | budget_exhausted / unresolved | 在停止后继续隐式调用模型 |

初版只保留确定性决策优先级：执行任何动作前检查停止请求、资源与预算；允许继续时先处理输入/环境问题，再处理明确反例；重复失败必须换有依据的动作。暂不训练动作策略，也不引入多 Agent 互相投票。

空转指纹由基准快照、规范化 diff、验证协议和稳定失败签名组成。只有字段一致才标记重复；随机或偶发失败仍需按协议的复跑规则判断。环境重试、模型重试均有独立上限并计入总墙钟与实际调用消耗。

## 3. 四类可迁移规则卡

规则卡是后续 Skill 的知识来源，不包含案例编号到答案的映射。未知语义进入 missing_evidence，不能由通用模板代替业务判断。

W07 已把下面四张规则卡的压缩执行提示登记在 `policies.py`，通过 `consult_rule` 访问；未知类别或关闭规则开关时返回通用路径。修复方向、义务、区分问题与实际失败的绑定统一见第 1.2.3 节；Agent 当前只授权已有日志/源码读取，没有诊断执行探针。下面条件是规则知识，尚未实现为可执行检查器，不能把提示命中解释为条件已满足或根因正确。

义务记录由模型工具创建时固定为 `model_inference`，须引用已登记诊断证据；候选的 restores/preserves 只能引用本次运行、本源码版本已登记的义务。合成检查实际失败后，模型可把失败与候选义务关联为 `scope=synthetic`、`link_authority=model_inference` 的反例；观察文本来自执行器，违反哪项义务仍是模型解释，不构成硬拒绝或应用行为证据。反例只关联原候选哈希及源码版本。可信需求、独立业务判据和应用反馈接入后，才能提高证据层级。

### R1 初始化与生命周期

- 触发：空对象解引用、未初始化字段被消费，或异步/生命周期顺序可疑。
- 提取：对象首次赋值、实际使用点、分支条件、生命周期回调及相关异步依赖。
- 条件：在需要该对象的每条目标路径上，使用前已经建立符合要求的值；异步初始化须有相应等待或明确状态处理。
- 区分：根本未初始化、初始化顺序错误、合法空结果、对象过早释放。
- 候选：调整依赖顺序、补齐必要初始化或明确空结果路径；不默认以可选链消除故障。
- 反例义务：首次打开、重复进入、空数据和适用的异步完成顺序；原功能实际发生，不能只检查不抛异常。
- 退出：业务是否允许空值无法确定时保留未知，先查消费方与需求。

### R2 索引、字节与元素边界

- 触发：数组/视图访问、DataView 读写或 TypedArray 构造相关错误。
- 提取：长度、下标、byteOffset、byteLength、元素宽度、步长、循环终止条件及下游格式。
- 条件：数组读取所需的索引范围应符合调用的业务前提；DataView 的读取满足 offset ≥ 0 且 offset + width ≤ byteLength；TypedArray 的偏移须按元素宽度对齐，显式长度满足 offset + count × width ≤ byteLength，省略长度时剩余字节数须可整除元素宽度。
- 区分：错误容量、错误单位、循环多一次、错误视图类型、上游数据不完整。
- 候选：修正容量/单位/终止条件，或在确有语义依据时改变处理类型；不默认截断尾部、缩短视图或吞掉错误。
- 反例义务：空输入、最小有效输入、边界前后、合法完整数据及尾部数据；按协议核验结果内容或格式。
- 退出：平台接口数值转换等细节不明确时查对应版本规范；不把这张常见条件卡冒充完整语言语义验证器。

### R3 递归终止与关系遍历

- 触发：深递归、重复调用、循环引用的关系遍历。
- 提取：递归参数变化、next/parent/child 关系、结束条件、输出顺序及重复节点含义。
- 条件：有限输入上的遍历须能停止，并满足约定的输出集合/顺序；终止条件的恢复不得删除必要工作。
- 区分：自环/多节点环、错误终止条件、合法但过深的无环链、共享节点造成重复访问。
- 候选：路径环检测、符合业务语义的访问集合或迭代遍历；不能直接把路径级集合换成全局去重。
- 反例义务：无环链、自环、多节点环、分叉/汇合及长链；分别检查终止与内容。
- 退出：重复节点是否允许未知时先查询调用方消费逻辑；“加深度上限”只可作为有明确降级语义的方案。

### R4 动态正则与解析

- 触发：动态构造表达式、格式解析抛错，或输入拼接可疑。
- 提取：原始输入、表达式构造、flags、转义过程及产品是否支持正则。
- 条件：构造语法有效，匹配/解析结果符合输入语义；转义正确不等于业务正确。
- 区分：字面查询误当正则、拼接语法错误、确实允许的正则输入无效、编码/格式不匹配。
- 候选：字面查询使用适当的字面匹配/转义，正则功能保留合法模式并定义无效模式行为；不默认 catch 后返回全部或空结果。
- 反例义务：普通文本、特殊字符、空查询、合法复杂输入及明确无效输入；核验实际结果集合。
- 退出：产品语义未明确时，先查 UI/调用方与已核验需求，不能只依据错误名称决定修法。

## 4. 用现有材料检查设计能否表达问题

下表来自此前静态阅读，作为设计推演而非独立效果评测。准确行号与来源以 [数据核查报告](preparation/data-b-assessment.md) 及固定源码版本为准。

| 代表材料 | 设计必须表达的区别 | 下一条有用证据 | 不能接受的表面成功 |
|---|---|---|---|
| BookRead2 / 004 | 使用位置与较晚的初始化位置不同 | 初始化依赖及页面加载应产生的内容 | 跳过加载后不崩溃 |
| ComprehensiveTool1 / 001 | 固定索引访问与集合实际长度的关系 | 集合来源及为何选择该项 | 随意换成第一项即可运行 |
| FitnessCenter / 015 | 正则可构造与搜索语义正确不同 | 查询是否支持用户正则及结果预期 | catch 后返回空列表 |
| Exam1 / 019 | 框架栈帧、业务递归与关系数据不同 | 环结构、输出顺序和重复节点的约定 | 限制递归深度后遗漏学习条目 |
| AudioEditing1 / 021 | 循环边界、读取宽度和帧步长共同作用 | 声道/帧结构及完整音频的输出约定 | 少读取一些数据后不越界 |
| HouseholdService1 / 022 | buffer 字节容量与元素个数不同 | 同一 payload 的生成、校验与其他消费者 | 只让视图构造通过，校验结果却错误 |

这些推演说明我们需要诊断、业务约束和反例三部分；它们不证明增强方法优于一个强基础 Agent。方法收益仍需运行对照。

## 5. 本机资源与清理契约

本节规定目标字段和行为；目录与资源机制见开发设计第 9 节，数值只读取 configs/local-runtime.json。LocalOwner、CleanupPlan 与 CleanupReceipt 已由 W01 的 PowerShell 管理入口部分实现；W02/W03 已为合成、Init、测试与质量 Run 建立 Job 限制、活动标记与生效限额记录。下表完整的 ProcessLease/EffectiveResourcePolicy/LocalUsage schema、正式 Agent 和本机应用作业接入仍待实现，现有 runtime.py 不包含这些能力。

| 对象 | 必须字段 / 约束 |
|---|---|
| LocalOwner | schema_version、project_instance_id、repo_real_path、managed_real_path、created_at；路径与当前仓库匹配，不把 owner 文件当任意删除权限 |
| EffectiveResourcePolicy | policy_sha256、解析后的绝对路径、生效的 CPU/提交内存/进程数/墙钟限制、磁盘预算与预留线、enforcement_capabilities；无效范围/未知字段拒绝，不接受 NaN/负数或大于 1 的比例 |
| ProcessLease | project_instance_id、run_id、Job 标识、supervisor PID/创建时间/路径、进程成员、启动时间、截止时间、状态；PID 必须和创建时间一起核验，Job 句柄只存在于运行进程 |
| LocalUsage | 测量时刻、各类字节、磁盘空闲、可用物理内存、受管 committed memory、峰值与执行能力；逻辑大小不能冒充精确可释放物理空间，未知不填 0 |
| CleanupPlan | plan_id、实例 ID、scope、规范目标列表、估计字节、保留项、证据导出状态、本机未结构建/设备动作及历史远程标记、目标身份/状态摘要；Apply 前重新核验，不能直接信任可编辑 JSON 中的路径 |
| CleanupReceipt | 实际删除/保留/失败项、时间、关联 plan_id、剩余占用；部分失败不记完整成功，重复执行可安全恢复 |

受管根只接受设计指定的仓库内目录。拒绝空路径、卷根、仓库根、上级/同前缀目录、路径链中的链接与目录外链接目标；管理根内部的 uv/Python 链接须解析核验并只删除链接本身。解析失败则不清理。清理流程持有独占维护锁并阻止新工作；进程或设备动作未结束、文件仍在用、证据未处理或旧远程作业状态未知时返回阻塞原因。测试应覆盖扫描之后发生路径替换的情况；本机同用户恶意篡改不能仅靠应用层路径检查获得完全隔离保证。

资源中断不得归因成补丁功能错误：result 使用 inconclusive 或 blocked，并记录 failure_kind=resource_limit / timeout / interrupted 及原因；已产生候选和所有成本保留。任务停止归入 budget_exhausted（资源/时间上限）、blocked（保护器不兼容或环境不可用）或 unresolved（用户停止），不新增另一套成功语义。操作系统内存上限触发可能导致分配失败而非自动退出，管理器仍须检测并结束/报告。

Stop 不隐式删除文件；Clean 不隐式杀未知进程；删除 artifacts/state 前要有可核对导出或用户明确丢弃，不能先清证据再补报告。受管 Job 外的既有设备服务须单独核验归属和停止能力；云端模型请求已发送但本机失联时仍按模型账本保留未知费用与结果，不假定 Stop 能撤销请求。

## 6. 关联与待完成规格

工作包顺序、具体未闭合项和完成条件统一见 [实施计划](project-work-plan.md)；实现进度见 [STATUS](STATUS.md)，对照与贡献归因见 [评测口径](preparation/metrics-and-gates.md)。因果记录到修复决策及候选的固定响应交接已可执行；运行条件检查、可信应用验证对象接纳与本机执行控制仍待闭合和实测。
