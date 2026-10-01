# 模块契约与规则规格

状态：当前规格草案，部分契约已有隔离合成实现。最后维护：2026-10-01。
职责：维护对象不变量、控制器分支与规则卡。配套 [总体技术设计](development/system-design-draft.md)。因果诊断、修复义务、有限条件结果、修复决策与候选的交接、模型账本、逐工具授权和动作恢复已接入固定响应离线循环；应用接受门、模拟 HTTP 协议及策略分支已有合成实现。可信采值、算子适用性和因果关系的实际检验、真实模型、可信业务判据和本机应用执行仍待实现。文中案例推演属于分析人员材料，不输入未来的独立评测求解器。

## 1. 数据契约与不可破坏的条件

离线原型与首批领域对象分别校验，不能把 evidence_ready 报告直接当作 Diagnosis。领域记录 schema_version=1 已包含下述因果字段；有些可空，以容纳尚未查明的内容。诊断提交本身不产生根因确认；应用打包与验收对象仍有独立契约。

### 1.1 版本与来源

Evidence、Diagnosis、RepairObligation 和 DecisionRecord 等 Record 派生对象包含 schema_version、task_id、run_id、record_id 和 created_at；ConditionRecord 的任务/运行/源码身份位于嵌套请求范围，内容身份使用 result_id，见第 3.0 节。涉及源码的记录绑定 source_snapshot_sha256；分析源码和完整运行源码分别使用 analysis_snapshot_sha256、runtime_snapshot_sha256，不要求二者相等。二者之间通过文件清单、共享业务源码哈希及打包记录建立关联。

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

源码文本由主机按版本和行范围读取，日志核对原字节哈希后读取，模型只能提交推断和引用，不能自填执行事实。执行证据只从已登记候选的已保存失败检查产生，核对候选源码哈希与 execution_id；恢复引用旧执行依据也须明确相同 feedback_candidate_id。所有模型提交的假设保持 unknown、关系保持 model_inference；这不是禁止后续可信检查确认它们，而是防止模型自授确认。引用闭包不得缺项或循环，已有证据 ID 不得改写。诊断、证据和关联义务在同一个不可改写的文件中原子保存；新假设或修订使用新 ID。诊断接纳工具只检查结构、引用和来源，不能证明因果解释正确，尚不自动调用第 3.0 节的条件检查器。

### 1.2.1 修复义务和动作提案

RepairObligation 用 record_id 作为 obligation_id，包含 kind（runtime_condition/business_behavior）、statement、bindings、source_evidence_ids、hypothesis_ids、authority（verified_requirement/interface_rule/existing_behavior/model_inference）、checkability（executable/manual/unknown）、status、scope 和共同任务/版本身份。模型提交只接纳 model_inference/unknown；checkability 只是拟议检查方式，不证明检查已实现。Hypothesis 与义务双向关联；CandidatePatch 关联 restores 与 preserves。结构化义务随诊断包保存，简单 record_obligation 保存相同规范的独立记录；策略辅助对象 ObligationClaim 不作为新的文件格式。当前合成反馈只能关联候选已经声明的义务，关联解释保持模型推断；真实 Check/Result 与可信义务检查的接入仍待实现。

模型推断的义务只能作为假设，不得仅因某候选违反该推断就机械拒绝它。局部执行只能支持对应输入与路径，不自动证明全局成立。可信语义不足时明确 unknown。

`ActionProposal` 是模型意图，不是执行结果。对象绑定 task/run/分析源码版本，保存 action、target_ids、evidence_ids、candidate_id、reason、required_capability、observation_routes、unknown_route 和 estimated_cost_basis。`record_analysis` 表示登记诊断、义务、建议、有限条件计算或反馈，不冒充执行观察或源码编辑。诊断探针至少区分两个修复方向，观测映射须能改变方向，并保留“未命中预测”的 `retain_unknown` 分支。`authorize_action` 只用主机持有的任务版本、已登记引用、候选、能力和剩余预算给出允许/拒绝；模型自报的能力与成本不构成授权。`finish` 还须引用已通过独立接受门的候选。

固定响应 SDK 的同步工具调用已通过 `ToolAuthorizationMiddleware`：主机把工具名与参数映射成动作，校验参数、版本、已登记引用、候选和实际能力；可选 `proposal_json` 只能补充模型的理由与引用，不能改变工具对应的动作/能力或候选，工具实际引用也不能被省略。主机根据已记账的模型响应生成稳定 action_id；模型响应内的 call ID 元数据由适配器覆盖，不接受模型自授身份。已付费且保存的当前响应可在没有下一次模型调用额度时完成合法工具动作，但仍受独立工具调用预算、候选预算和截止时间限制。实际探针、停止请求、资源租约及应用结果归类仍待真实控制器集成。

`ToolActionStore` 在工具产生效果前记录授权与 `dispatched`，工具返回后保存完整 `ToolMessage` 及哈希再交给 Graph。`completed` 仅表示工具结果已保存，授权通过也不表示工具执行成功。相同模型调用与工具调用身份、相同参数的完成结果恢复时只回放；身份对应的参数改变则拒绝。已派发但无结果记录时保留未知，停止为 `action_result_unknown`，不盲目再次编辑。拒绝本身只有工具错误消息，不生成源码修改、候选或检查事实。原始工具结果只留受管 SQLite，报告只列授权、状态和结果哈希；这不是应用成功证据。

反例 Counterexample 保存实际输入或产物引用、执行版本、观察、violated_obligation_ids 及 affected_hypothesis_ids。关联本身也可能是模型推断，必须标记；只反驳观测所覆盖的主张。重试时优先携带有效反例与未知项，不重复携带所有长日志。

### 1.2.2 模型请求、响应与恢复

`ModelCallIntent` 是主机生成的稳定调用身份：call/task/run ID、调用用途（求解、格式修复、上下文压缩或 SDK 重试）、提供方与具体模型 ID，以及完整有序消息、工具 schema、生成参数的规范化哈希和允许的工具名。哈希对应的原始请求必须由可信适配器核对；仅填入任意哈希不能证明实际发送内容。调用次数从登记时占用，同一 task/run 内所有用途共用次数预算，不能把格式修复或压缩隐藏在 SDK 内部。跨 run 的总预算尚未定义。

`ModelCallStore` 用项目受管目录内的 SQLite 保存 `reserved → dispatched → completed` 或 `dispatched → unknown`。先持久化意图，紧邻外部调用前标记 dispatched；重启后已完成的同一请求回放已保存的 `AIMessage`，不再发请求。已发出但无完整本地响应时，同一 call ID 禁止重发，结果与费用维持 unknown；确需另试须显式建新 call ID 并另占次数。标记 dispatched 与网络实际送达之间的崩溃窗口也保守算 unknown。提供方通常不保证以 call ID 查询或幂等，不能承诺 exactly-once。异常捕获后的原因只记录类别，不把异常文本中的凭据写进账本。

收到响应后保存规范化消息及哈希、提供方消息 ID（如可得）、工具调用名称/ID/参数摘要、格式问题与 `usage_metadata` 的真实 input/output/total。原始消息仅保留在本机受管状态用于检查点落盘前恢复，不进入 Git 或默认汇总导出。格式错误也算已完成的一次调用，不能丢掉它的 usage；缺失或无效 usage 单独标记 `missing`/`invalid`，统计只有已知 Token 和未知调用数，不从文本长度补估。`text_only`、模型自称成功、工具参数或模型推断都不产生执行证据。固定响应 Deep Agents 模型入口已通过账本并把主机调用身份绑定到工具授权；Graph 检查点之前的完成响应可回放，已发出且未保存的响应停止为 unknown。真实 LangChain 提供方、其底层重试的逐次计费、实际 Token 硬限额和应用接受门接入仍待完成。

### 1.2.3 已登记诊断到修复决策

`plan_repair_action(plan_json=...)` 接收 `DecisionRequest`，由 `inspect_analysis(include_schema=True)` 提供 schema。请求绑定 task_id、run_id、diagnosis_id 和含源码版本的 PolicyInput；失败后再决策须成对传 candidate_id 与已保存的 feedback_id，不接受模型自填失败观察。旧的独立 PolicyInput 仍可供纯策略函数使用，已不作为 SDK 工具输入。

引用有限条件结果时须同时传 condition_result_ids 和完整 condition_scope；不允许仅凭结果 ID 猜测用途。主机重新核验已保存结果，要求同诊断、同分析版本、完全相同的候选/操作/场景/路径/对象/采样点，且结果对应的假设属于提议方向。若同时使用候选失败反馈，条件范围须属于该反馈候选。原版结果可用于原版范围下提出新修法，但不得作为新候选的条件结果；候选条件无需伪造 feedback_id，直接在 condition_scope 中指定候选。

每个 RepairDirection 包含 direction_id、hypothesis_ids、support_evidence_ids、obligation_ids、repair_key 和 acceptance_key。方向须引用本诊断的假设和证据，包含相关假设已关联的义务；义务须与方向假设或同包依据有关。检查的 outcome→方向映射须对应已登记 DiagnosticQuestion 的 outcome→假设映射；类型和能力须匹配。这里只核对引用，不能证明证据足以支持方向。repair_key/acceptance_key 相等是模型声明的修法/验收等价，未通过补丁或业务行为验证。

主机覆盖模型填写的可用能力和探针预算：离线路径仅保留主机实际提供的 read_source/read_log，执行探针预算为零。同修法且同验收直接建议候选；不同修法须有区分力、能力和预算才建议补查；否则允许带 unknown 尝试或 unresolved。建议本身不读取源码或执行探针，实际读改查仍经过共享工具授权与预算。

有效反馈必须来自同任务已冻结候选的已保存失败合成检查，匹配候选源码哈希、execution_id 和原观察。引用另一诊断时该诊断须明确 feedback_candidate_id。失败关联的义务必须已由原候选声明；scope=synthetic、evidence_kind=local_execution、link_authority=model_inference。只重新考虑相关方向，不硬拒绝推断义务，不把失败解释升级为已证实的因果关系；关闭增强时保持一般未知路径。

有限条件关联单独保存为 condition_review，不使用失败执行 Counterexample 的来源语义。开启 obligation_tracking 且引用结果为 violated 时，只建议重审同假设的方向；satisfied、unknown 或关闭关联时保留未知。原版条件被违反不等于某个修法已失败；算子/绑定适用性仍是推断，不自动确认根因、满足业务义务或硬拒绝候选。基础与增强均可计算、保存和回读相同条件，增强开关只控制关联建议。

`decisions.py` 保存不可改写的 DecisionRecord：规范化请求、诊断/义务/反馈/条件上下文哈希、策略开关、原始策略建议、反馈建议、condition_review、最终 next_action 及推断层级。身份摘要排除接纳时间等审计元数据，但保留语义输入；相同请求和上下文回读原结果。回读重核条件引用和建议，不接纳改写的关联方向或动作。inspect_analysis 可列出决策/反馈/条件 ID 并按 ID 回读；报告包含这些记录。保存决策属于 record_analysis，不冒充执行检查。

候选可选成对引用 decision_id/direction_id；主机核对同诊断、已提议方向和候选声明义务。决策是建议，允许模型选择其他已提议方向；未知建议本身不阻止合法候选登记。自动根因确认和真实应用反馈尚未接入。

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

W07 已把下面四张规则卡的压缩执行提示登记在 `policies.py`，通过 `consult_rule` 访问；未知类别或关闭规则开关时返回通用路径。修复方向、义务、区分问题与实际失败的绑定统一见第 1.2.3 节；Agent 当前没有诊断执行探针。R1/R2 的部分有限条件已有第 3.0 节的合成计算工具，R3/R4 和业务语义仍是规则知识；提示命中不能解释为条件已满足或根因正确。

义务记录由模型工具创建时固定为 `model_inference`，须引用已登记诊断证据；候选的 restores/preserves 只能引用本次运行、本源码版本已登记的义务。合成检查实际失败后，模型可把失败与候选义务关联为 `scope=synthetic`、`link_authority=model_inference` 的反例；观察文本来自执行器，违反哪项义务仍是模型解释，不构成硬拒绝或应用行为证据。反例只关联原候选哈希及源码版本。可信需求、独立业务判据和应用反馈接入后，才能提高证据层级。

### 3.0 首批有限条件检查

`conditions.py` 提供独立程序接口及固定响应 SDK 的 evaluate_condition 工具。它回答“这组有界输入是否满足指定条件”，不回答“源码一定产生这些值”或“业务已经修好”。不解析或求值 RuntimeConstraint.condition、ValueBinding.expression；只使用已登记绑定 ID 和有限枚举算子，不作为 CheckLedger 执行器。

ConditionRequest 包含 check_id、diagnosis_id、hypothesis_id、kind、binding_ids、length_mode、profile 和 ConditionScope。范围绑定 task/run、分析源码哈希、实际计算版本、可选候选 ID、该版本的 operation 位置、scenario_id、path_id、subject_id 和 sample_id。主机核对诊断及假设的规则类别、绑定存在性、原版操作位置、基准实际字节和冻结候选实际字节。候选位置校验路径/行范围，尚不自动证明原版与候选行映射；模型选错算子或绑定含义也不会被此校验自动纠正。

| 算子 | 角色/单位 | 有限计算 |
|---|---|---|
| array_index | index、length 为 elements | 0 ≤ index < length；这是需要集合内元素的条件，不声称普通数组越界必抛异常，也不检查稀疏槽内容 |
| data_view_access | capacity、offset 为 bytes，width 为 bytes_per_element | offset + width ≤ capacity；capacity 指视图可访问字节数，允许未对齐访问 |
| typed_array_view | capacity、offset 为 bytes，width 为 bytes_per_element；显式 count 为 elements | 偏移对齐；显式长度不超过容量；remaining 模式无 count，检查偏移范围及剩余字节整除 |
| ready_at_use | state 为 state | 同一对象、路径和使用点的状态为 ready；uninitialized/released 违反条件，未采于使用点或未知状态保留 unknown |

finite_condition_v1 仅接纳已归一化的安全整数，非 index 数值非负，字节宽度限于当前支持的固定宽度集合；DataView/TypedArray 还须确认 buffer 固定且未脱离。不执行语言数值转换，不覆盖可变/共享 buffer、并发状态变化或完整 ArkTS 语义。2026-10-01 查阅 [ECMAScript 2023 TypedArray 构造规则](https://tc39.es/ecma262/2023/multipage/indexed-collections.html#sec-initializetypedarrayfromarraybuffer) 和 [ECMA-262 第 14 版 DataView 规则](https://dev.ecma-international.org/wp-content/uploads/ECMA-262_14th_edition_june_2023.pdf)：用于有限公式与前提，目标 HarmonyOS 版本兼容性未验证。生命周期算子仅检查采样状态，不模拟异步执行或证明所有路径初始化。

ObservationBatch 包含 observation_id、上述完整范围、origin、值/单位和明确的归一化、buffer、采样前提。SyntheticObservations 只由主机测试/准备代码注入，复制规范化 fixture；模型不能通过 origin 标签、同 ID 改值或自填哈希取得来源资格。当前 registry 固定的是测试输入，不是运行观察；没有源码值提取器、实际运行采样器或应用来源接纳接口。输入范围不一致、值缺失、单位错配或前提未知返回 unknown；非法请求、诊断引用、规则绑定或已变源码直接拒绝。

ConditionResult 保存请求和输入摘要、每个子条件及操作数、satisfied/violated/unknown 和 synthetic/unknown 支持层级。状态只描述该组 fixture 输入的计算；applicability_authority 固定 model_inference，root_cause_confirmation 固定 unknown，application_verification 固定 not_run。结果不会改写诊断、义务或共同接受协议，不产生 CheckLedger pass/fail，也不把局部条件满足当作业务正确；后续决策只按第 1.2.3 节受控引用。

ConditionStore 将主机输入保存到工作目录的 condition-inputs.json，将 ConditionRecord 保存到 conditions/<result_id>.json。记录绑定结果内容、输入 registry 指纹和去除接纳时间后的诊断包哈希，result_id 为内容摘要；相同输入回读原记录，不覆盖旧结果。回读校验归属、文件类型及链接，重新核对诊断、源码版本和完整计算；即使刷新文件名/摘要，也不能把不满足的谓词改成满足。持久 fixture 可支持恢复，仍不证明实际运行来源。

evaluate_condition 只收序列化 request_json、可选 observation_id 和动作提案；不接受模型自填值、fixture 或结果。主机可通过程序接口注入合成 registry；当前 CLI 默认空 registry，未提供 ID 时保存 missing_observations 的 unknown，未知 ID 拒绝。inspect_analysis 可列出输入 ID/范围和结果 ID，按 condition_result_id 回读，并按需返回请求 schema。计算登记属于 record_analysis，引用与候选纳入相同工具授权和总工具预算，不消耗一个虚构的执行探针额度。session schema 5 固定 registry 指纹；恢复不能替换输入，已保存工具回执则回放，不重复登记动作，回执未存时保持动作结果未知。报告 schema 3 增加 condition_results；旧会话 schema 1–4 不续跑。可信值采集、目标版本适用性和真实应用验证尚待实现。

#### 3.0.1 可信采值与算子绑定

本节定义可信采值与操作绑定的契约。**独立的原字节锚点、主机合成绑定、采值请求/凭据和不可改写登记已实现；真实语法/类型绑定、literal 提取、执行采样及新来源条件计算仍未实现。** 上节 schema、SDK 工具和证据能力保持原有行为。完整目标是分别核对“值从哪里取得”和“值属于哪个操作的哪个角色”，按已冻结源码建立绑定，按该绑定取得凭据，再计算条件；模型提出问题和引用，不能提交主机确认状态、可信值或执行凭据。

##### 来源与结论分层

| 来源 | 接纳依据 | 可支持的结论 | 保持未知的结论 |
|---|---|---|---|
| source_literal | 主机读取原字节，核对参数槽的精确 literal span、解析器支持范围及绑定 | 该源码参数槽存在此整数文本；在已声明前提下计算源码输入公式 | 实际取值、分支触达、对象/时序、业务和应用效果 |
| static_inference | 显式推导规则、前提和 source_fact/model_inference 依据；本包不实现数据流推导器 | 在列明前提下的候选值、区间或缺口 | 未验证的推导不能因由主机保存就成为 source_fact 或运行值 |
| synthetic_fixture | 主机固定测试输入及指纹 | 合成输入上的确定性计算与恢复测试 | 数据真实分布、目标代码实际取值、运行或修复效果 |
| execution_sample | 已登记受管作业、实际运行/观察版本、原始产物及可信采样适配器 | 指定操作事件、对象和路径上的局部值/状态 | 所有路径成立、自动确认因果、业务/回归通过、最终修复 |

源文件中的 literal 和一次使用点的值分别记录。源码凭据不得伪造 observed_scope 中的运行事件、对象或场景；请求中的范围只表示查证意图。原日志中缺乏对象/操作事件关联的数值仍是 log_observation，不直接成为运行采值。合成执行替身无论是否提供 execution_id，都保持 synthetic；适配器名称、producer 字符串或自填哈希不产生来源资格。

##### 接口、对象和主机职责

完整调用顺序为 `bind_operation(proposal) → BindingAssessment`、`capture_values(request) → CaptureReceipt`、`calculate_condition(binding_id, receipt_ids) → ScopedConditionResult`。前两步已有独立合成 Python 入口，最后一步仍拟议。下表定义完整字段职责；本包只实现其中的合成子集，不能将字段名或模拟事件当作真实采样能力。

| 对象 | 必需信息与义务 |
|---|---|
| BindingProposal | task/run、诊断/假设、已冻结分析或候选版本、拟议算子和源码位置、诊断 binding_id 引用；不含值、主机角色映射或确认状态 |
| SourceAnchor | 实际源码快照、规范路径、文件原字节哈希、UTF-8 字节 start/end（半开区间）、片段摘要、父操作锚点；展示行号单列。严格解码，不通过换行归一化改变偏移 |
| BindingAssessment | 主机生成的 binding_id/内容哈希、适配器身份/版本、绑定支持来源、算子/length_mode、父操作和各 operand anchor、role→operand→诊断 binding_id 映射、类型/作用域依据、语义 profile、绑定状态和 unknown_reasons。状态为 verified/unknown；verified 须有适配器实查的操作形状、API/类型依据和角色映射，不能由模型声明。合成替身的 verified 只表示 synthetic 前提下结构一致，不取得源码或实际执行资格 |
| SemanticProfile | 有限公式版本、规范来源/摘要、支持的 API 与类型、转换/buffer/采样前提、匹配的目标 SDK/语言运行时与核验依据、平台适用状态。数学公式来源与目标版本兼容性分别核对，后者不足时为 unknown |
| CaptureRequest | 已登记 binding_id/哈希、source_literal 或 execution_sample 采值方式、拟议场景/使用点、执行方式所需的已有作业或探针计划 ID、查证理由；不含观测值、实际事件、来源确认或自报预算 |
| CaptureReceipt | 主机内容寻址 ID、原请求/绑定哈希、来源、适配器身份/版本、原始产物摘要、采集状态、值/单位/各自 anchor 及前提依据。源码来源仅有源码范围；执行来源另有下述 observed_scope，缺失数据不补默认值 |
| ScopedConditionResult | 输入 binding/receipt 内容哈希、每个谓词及 calculation_status、输入来源支持、绑定状态、平台适用性和未知理由；root_cause_confirmation 仍 unknown、application_verification 仍 not_run |

CaptureReceipt 的采集状态为 collected / not_reached / blocked / inconclusive / unknown。只有 collected 且所需角色完整、来源及前提满足才产生该范围的计算支持；状态并不表示条件成立。数学结果 satisfied/violated 与平台适用性可以不同：确定的一组数字可算出谓词，而目标运行语义仍未知。static_inference 不进入已观察值的接纳入口；模型仍可表达假设，但不能用假设填运行凭据。

当前 `value_sources.py` 只接受由程序提供的 `SyntheticValueAdapter`。主机固定 `SyntheticBindingFixture` 的操作/角色/参数 span 与诊断引用；`bind_operation` 实读冻结源码的原字节，检查 UTF-8 字节边界、文件/片段摘要、原行号和父操作包含关系。没有支持的 fixture 时返回 unknown；即使状态 verified，其 support_origin 仍为 synthetic_fixture、目标平台仍 unknown，不说明程序已解析或运行这个操作。

`request_capture` 先登记内容寻址的 CaptureIntent；`capture_values` 使用另行由主机登记的 SyntheticCaptureFixture，模式可模拟 source_literal 或 execution_sample，但 origin 固定为 synthetic_fixture。逐角色核对绑定/anchor/单位，缺值、数值 profile 或前提不足保留 unknown；模拟事件分别核对操作、任务/候选和拟议范围，并要求独立 object_instance_id、object_generation、同次 occurrence/线程与 at_use 阶段。异时或异对象事实保留但不拼合，目标未到达/中断状态可回读且不升格。这里没有设备派发、预算放宽、源码 literal 自动解析或有限条件计算。

保存目录为工作目录内 `value-sources/` 的 manifest.json、bindings/、requests/、receipts/。静态 manifest 固定合成 adapter/profile/policy，动态 fixture 与 receipt 各自固定内容身份；追加不改变旧记录。重开须继续提供可信主机 adapter 及相应历史 fixture；不从磁盘标签自动建立信任。登记采用同目录临时文件与排他发布，绝不覆盖目标；binding/intent/receipt 回读均无登记副作用，并重新实查源码、诊断语义和主机 fixture。链接、硬链接、内容改写、缺失/替换 manifest 或内容寻址自洽但与主机依据不符的伪造都拒绝；中断留下的不完整记录不能自动视为已完成。

主机接纳入口须实查原始文件或作业/产物，重新产生摘要和角色映射，再保存不可改写对象；模型 JSON 只能选择已有 ID 或提交 proposal。测试适配器明确属于 synthetic 通道，不允许用伪造作业模拟得到 local/application 来源。既有 Evidence 的 source_reader/execution_backend 只在各自真实依据存在时使用，不通过扩展 ObservationBatch.origin 把旧 fixture 升级。

##### 算子与参数角色

单位校验是必要条件，不能代替结构/对象绑定。角色映射由支持的操作适配器产生，模型再次计算时必须引用这一映射，不得交换同单位的角色或改用另一算子。

下表的“实际对象/同次使用事件”是 execution_sample 的运行核验要求。源码阶段只核对参数槽、API/类型和结构关系，不声称运行对象或状态已知，也不要求为读取 literal 先运行应用。

| 算子 | 必须核对的角色来源 |
|---|---|
| array_index | index 为这次索引访问的下标槽；length 为同一集合、同一次使用事件的长度，不绑定另一集合或任意源码数字 |
| data_view_access | capacity 为实际 DataView 可访问长度；offset 为这次读写的偏移参数；width 来自已确认读写 API/profile，不能拿 backing buffer 长度替代 view 长度 |
| typed_array_view | capacity 为传入的实际 backing buffer 容量；offset/count 为构造参数槽；显式/省略长度按实际调用形状区分，width 来自已确认的元素类型/profile |
| ready_at_use | state 来自该消费操作实际使用的对象实例/代次及事件；初始化处、其他变量或调用完成后的状态不支持使用时就绪 |

首版源码提取只接纳已核对参数槽上的直接、安全整数 literal；负值、转换等是否满足有限 profile 分开检查，不自动截断或归一化。`const n = 4`、属性访问、函数结果或计算表达式不自动传播到使用点；字符串、注释和重复片段中的相同数字不属于已绑定 operand。未知解析形状、内置名称遮蔽、包装器和缺类型/作用域依据保留 unknown。不使用 eval 或执行被分析代码来提取值；完整 ArkTS 解析/类型前端不属于第一实现包。

候选重新核对实际字节并生成新绑定。旧 SourceLocation、近似 diff 映射和相同片段只用于查找，不能继承原版绑定资格。重新定位存在多个父操作候选时保存歧义；无法确认参数含义时回通用修复，不为了产生 verified 随机选取。即使唯一新位置已确认，也只证明候选的新操作绑定；原版与候选是否业务等价仍未知。

##### 执行采样与观察扰动

执行 observed_scope 必须包含实际 execution_id/job_id、原版或 candidate_id、分析到完整运行源码的打包依据、runtime_source/运行产物摘要、environment/initial_state、scenario、路径事件、进程/线程或调用上下文、对象实例及代次、operation occurrence、事件阶段与一致性依据。sample_id 由主机按事件身份生成；模型的 subject_id/scenario 标签不能代替这些事实。同名变量、复用地址或不同时刻的值不可拼成一次操作。

“使用点”指经适配器核验的操作边界：本次原表达式已求值一次，采样与操作使用的是同一组参数/对象，且没有未核对的转换或状态变化间隙。禁止为采值再次调用函数/getter、提前取值或多次做类型转换。固定/未脱离 buffer、归一化整数及状态时序须有适配器/profile 依据；可变/共享 buffer、并发修改、语义未知的转换或不能保证事件一致性均保留 unknown，不静默按普通固定 buffer 处理。

探针修改在独立观察副本中进行，不能覆盖原冻结候选。ProbePlan 固定目标 binding、采样方法/适配器版本、探针源码/配置摘要、观察副本与原运行版本的映射、扰动检查及其证据；运行产物绑定 observation_runtime_sha256。只有插桩构建成功而目标未到达时，记录 not_reached，不判条件满足。探针消除了原故障或改变语义时记录 inconclusive/unknown；它不是业务修复。实际支持哪些采样机制、能否观测转换后的参数及非扰动证据，待 W08 的工具链/设备能力核验，不预设 HarmonyOS 已提供某个调试接口。

首版不把多事件、多对象或多个来源的片段拼成运行 batch；一张执行凭据须包含同次操作所需角色和一致性依据。源码 literal 可作位置/参数旁证，不能填补执行凭据中缺失的值。来源事实有效但前提不足时保存事实与 unknown，不伪造 collected 全量数据。

##### 预算、保存与恢复

主机先核对来源能力、版本、对象引用、资源和预算，再登记请求。源码事实读取使用已有证据读取能力；新运行采样须走诊断执行授权、探针预算及受管作业，既有采样产物回读不能悄悄启动新作业。计算不启动采样器，也不代扣或放宽运行额度。所有读取、采集、解析、检查和失败都进入共享工具/时间/实际模型或设备成本；基础与增强拥有相同来源工具，差异只在何时使用和怎样关联。

完整采值 store 固定适配器能力/版本、语义 profile 和探针策略的 manifest；每个绑定、请求、凭据和结果独立内容寻址、追加登记。真实值在运行后才产生，所以不把整个动态 receipt 集合作为会话启动时必须冻结的 fixture 列表。会话冻结上述静态 manifest，计算身份再固定实际 receipt 哈希；既有凭据不得修改，新增凭据不改写旧计算。源码回读重新实查字节；执行回读核对作业与产物记录及适配器，不能靠重算算术补出执行来源。

采集请求先持久化；完成凭据可恢复回读，已派发但无法对账的执行保持 inconclusive/unknown，不重复产生设备动作。停止和清理沿用本机租约/证据保留规则；观察副本、探针产物和 receipt 均在受管工作目录，不为此建立常驻服务或第二套任务台账。

##### 拒绝与未知的反例验收

非法归属、已改字节、伪造凭据或与已登记映射矛盾属于拒绝；合法请求但能力/解析/前提不足保存 unknown，不当作反驳。首个实现包至少覆盖以下独立反例，测试替身均只产生合成证据。

| 反例 | 必须行为 |
|---|---|
| capacity/offset 或 index/length 调换，单位相同 | 与主机角色映射不一致，拒绝；不产生可信计算 |
| 已登记 TypedArray 操作被改用 DataView 谓词，或显式长度被改成 remaining | 算子/调用形状与 binding 不一致，拒绝；不能只校验 binding_id 存在 |
| 同一行有多个操作，模型指向另一个参数/注释数字 | 不接纳错误 anchor；支持范围不足或定位多义时 unknown |
| 候选插入行、改写调用、出现重复片段 | 旧绑定不得沿用；重新实查产生新绑定，歧义保留 unknown |
| 构造器被遮蔽、API 被包装或 profile 与目标版本未匹配 | 绑定或平台适用性 unknown，不凭名称宣布内置语义成立 |
| 初始化 literal 存在，但使用点变量已改变或来源未知 | 只保存文本事实，使用点值 unknown，不执行传播猜测 |
| 其他候选/任务/作业/场景的运行值 | 已知归属矛盾拒绝；缺少归属依据 unknown |
| 对象释放后实例地址复用、相同变量换对象 | 实例/代次不一致不得组合；缺代次证明时 unknown |
| 使用前未就绪或使用后就绪，被当作使用时状态 | 保留原事件事实，ready_at_use unknown；正确使用点事件才可计算 |
| 参数来自多个时刻/线程/对象，期间状态变化 | 不拼合；一致性依据不足 unknown |
| 目标未触达、作业失联或资源中断 | not_reached/inconclusive/blocked，条件 unknown，非修复失败/成功 |
| 为记录重复执行 getter/转换，或观察副本消除故障 | 不给可信使用点值资格，inconclusive/unknown，不把探针当修复 |
| 数值/单位/固定 buffer/归一化前提不满足 | 有效事实保留，有限计算 unknown；不补值或放宽 profile |
| 模型自填 producer/hash/status 或把 mock 作业标真实 | 接纳入口拒绝；synthetic 通道不能升级 |
| receipt、绑定、profile 或源码被改写；跨恢复更换 manifest | 拒绝旧身份复用，保留已存证据与未决项 |

##### 与现有实现的迁移

当前独立合成入口不升级既有 Contract/schema 1、ConditionStore、report schema 3 或 session schema 5，也不开放新 SDK 工具、真实来源或运行能力。既有 synthetic/unknown 路径与测试继续有效，既有 applicability_authority=model_inference 保留；新来源不能靠给旧结果增加一个“可信” sidecar 接入。

独立绑定/采值凭据契约与合成接纳门单独回读，不直接注入现有 ObservationBatch。真正接入新来源时先显式设计独立来源对象的新 schema；改变计算/SDK 工具语义时再设计 ConditionRequest/Inputs/Result/Record schema 2 和对应新 session/report schema，冻结新增 manifest。新会话不跨旧工具 schema 续跑，旧记录仍按旧规则回读为历史合成结果。旧 fixture 若引用新绑定，须登记新的请求并重新计算新 ID，来源仍 synthetic。禁止覆盖旧结果、直接修改 v1 封闭来源枚举或让 v1 默认接纳真实执行标签。

根因假设仍由证据和因果检验支持；可信局部采样也不自动改变 Diagnosis/RepairObligation 状态。ScopedConditionResult 不是 VerificationVerdict，不进入应用接受门充当 pass。共同协议的原故障复现、构建/目标/业务/回归检查仍由独立验收和实际运行提供。

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

工作包顺序、具体未闭合项和完成条件统一见 [实施计划](project-work-plan.md)；实现进度见 [STATUS](STATUS.md)，对照与贡献归因见 [评测口径](preparation/metrics-and-gates.md)。因果记录、有限条件结果到修复决策及候选的固定响应交接已可执行；真实值采集、其余规则检查、可信应用验证对象接纳与本机执行控制仍待闭合和实测。
