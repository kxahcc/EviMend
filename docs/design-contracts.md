# 模块契约与规则规格

状态：当前规格草案，部分契约已有隔离合成实现。最后维护：2026-10-05。
职责：维护对象不变量、控制器分支与规则卡。配套 [总体技术设计](development/system-design-draft.md)。因果诊断、修复义务、有限条件结果、修复决策与候选交接、模型账本、逐工具授权、动作恢复和主机终止摘要已接入固定响应与可配置 mock wire 的共享离线循环；应用接受门、模拟 HTTP 协议及策略分支已有合成实现。可信采值、算子适用性和因果关系的实际检验、云端模型、可信业务判据和本机应用执行仍待实现。文中案例推演属于分析人员材料，不输入未来的独立评测求解器。

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

`diagnostics.py` 已将上述对象接入两种离线模型共用的工具。`inspect_analysis(include_schema=True)` 按需提供主机任务身份、DiagnosisSubmission 的 JSON schema 和接纳规则；按 diagnosis_id 可回读原记录。`record_diagnosis(diagnosis_json=...)` 接收诊断、源码/日志引用、模型推断、已有证据 ID、失败检查引用及义务；旧的 path/line/reason 简单路径仍保留，不能和结构化路径混用。

源码文本由主机按版本和行范围读取，日志核对原字节哈希后读取，模型只能提交推断和引用，不能自填执行事实。执行证据只从已登记候选的已保存失败检查产生，核对候选源码哈希与 execution_id；恢复引用旧执行依据也须明确相同 feedback_candidate_id。所有模型提交的假设保持 unknown、关系保持 model_inference；这不是禁止后续可信检查确认它们，而是防止模型自授确认。引用闭包不得缺项或循环，已有证据 ID 不得改写。诊断、证据和关联义务在同一个不可改写的文件中原子保存；新假设或修订使用新 ID。诊断接纳工具只检查结构、引用和来源，不能证明因果解释正确，尚不自动调用第 3.0 节的条件检查器。

### 1.2.1 修复义务和动作提案

RepairObligation 用 record_id 作为 obligation_id，包含 kind（runtime_condition/business_behavior）、statement、bindings、source_evidence_ids、hypothesis_ids、authority（verified_requirement/interface_rule/existing_behavior/model_inference）、checkability（executable/manual/unknown）、status、scope 和共同任务/版本身份。模型提交只接纳 model_inference/unknown；checkability 只是拟议检查方式，不证明检查已实现。Hypothesis 与义务双向关联；CandidatePatch 关联 restores 与 preserves。结构化义务随诊断包保存，简单 record_obligation 保存相同规范的独立记录；策略辅助对象 ObligationClaim 不作为新的文件格式。当前合成反馈只能关联候选已经声明的义务，关联解释保持模型推断；真实 Check/Result 与可信义务检查的接入仍待实现。

模型推断的义务只能作为假设，不得仅因某候选违反该推断就机械拒绝它。局部执行只能支持对应输入与路径，不自动证明全局成立。可信语义不足时明确 unknown。

`ActionProposal` 是模型意图，不是执行结果。对象绑定 task/run/分析源码版本，保存 action、target_ids、evidence_ids、candidate_id、reason、required_capability、observation_routes、unknown_route 和 estimated_cost_basis。`record_analysis` 表示登记诊断、义务、建议、有限条件计算或反馈，不冒充执行观察或源码编辑。诊断探针至少区分两个修复方向，观测映射须能改变方向，并保留“未命中预测”的 `retain_unknown` 分支。`authorize_action` 只用主机持有的任务版本、已登记引用、候选、能力和剩余预算给出允许/拒绝；模型自报的能力与成本不构成授权。`finish` 还须引用已通过独立接受门的候选。

SDK 的两种离线模型共用 `ToolAuthorizationMiddleware`：主机把工具名与参数映射成动作，校验参数、版本、已登记引用、候选和实际能力；可选 `proposal_json` 只能补充模型的理由与引用，不能改变工具对应的动作/能力或候选，工具实际引用也不能被省略。主机根据已记账的模型响应生成稳定 action_id；模型响应内的 call ID 元数据由适配器覆盖，不接受模型自授身份。已登记且保存的当前响应可在没有下一次模型调用额度时完成合法工具动作，但仍受独立工具调用预算、候选预算和截止时间限制。实际探针、停止请求、资源租约及应用结果归类仍待真实控制器集成。

`ToolActionStore` 在工具产生效果前记录授权与 `dispatched`，工具返回后保存完整 `ToolMessage` 及哈希再交给 Graph。`completed` 仅表示工具结果已保存，授权通过也不表示工具执行成功。相同模型调用与工具调用身份、相同参数的完成结果恢复时只回放；身份对应的参数改变则拒绝。已派发但无结果记录时保留未知，停止为 `action_result_unknown`，不盲目再次编辑。拒绝本身只有工具错误消息，不生成源码修改、候选或检查事实。原始工具回执只留受管 SQLite；报告保留动作及诊断/候选/反馈摘要，不复制完整原始工具回执，也不产生应用成功证据。

反例 Counterexample 保存实际输入或产物引用、执行版本、观察、violated_obligation_ids 及 affected_hypothesis_ids。关联本身也可能是模型推断，必须标记；只反驳观测所覆盖的主张。重试时优先携带有效反例与未知项，不重复携带所有长日志。

### 1.2.2 模型请求、响应与恢复

`ModelCallIntent` 保持 schema 1，是主机生成的稳定调用身份：call/task/run ID、调用用途（求解、格式修复、上下文压缩或 SDK 重试）、提供方与具体模型 ID，以及完整有序消息、工具 schema、生成参数的规范化哈希和允许的工具名。固定响应入口摘要 LangChain 消息与工具定义；`AccountedProviderModel` 摘要实际 wire 消息、原生 function descriptors 和生成参数，并把规范请求体及适配器 profile 的 SHA-256 纳入参数摘要。仅填入任意哈希不能证明实际发送内容，发送边界仍须复核下述冻结请求。调用次数从登记时占用，同一 task/run 内所有用途共用次数预算，不能把格式修复或压缩隐藏在 SDK 内部。跨 run 的总预算尚未定义。

`model_provider.py` 的 `ProviderSettings` 冻结 provider_id、model_id、输出上限、可选 temperature 和请求超时；身份仅是配置，实际提供方/型号仍未选择。`ChatCompletionsAdapter` 当前只接收主机注入的 exact HTTPX MockTransport，端点固定为 model.invalid，无凭据、真实端点配置、自动重试或重定向。profile 包含 wire/schema、mock-only 标志、设置及请求/响应限额。2026-10-02 查阅 [DeepSeek Chat Completion 官方接口](https://api-docs.deepseek.com/api/create-chat-completion/) 作为文字/工具 wire 的设计参考，不表示完成该提供方或所有兼容 API 的接入。

`PreparedRequest` 冻结 canonical body、body_sha256 和 profile_sha256；messages/tools/parameters 仅是由 body 解析的独立视图，不能通过修改嵌套副本改变发送内容。发送前重核规范字节、哈希、profile 和有限请求结构，再发送相同 UTF-8 body。支持 system/user/assistant/tool 文字消息、原生 function 工具、stop、tool_choice 和明确的 parallel_tool_calls；assistant reasoning_content 若存在，按原值保存并用于续轮。tool ID 必须唯一，结果须对应尚未完成的工具调用；附件、未知角色/生成选项、未闭合工具历史及不支持的格式发送前拒绝，不静默丢字段。请求/响应限额以适配器常量及 profile 为准；非有限数、重复 JSON 键和不合法 Unicode 拒绝。

`ModelCallStore` 用项目受管目录内的 SQLite 保存 `reserved → dispatched → completed` 或 `dispatched → unknown`。先持久化意图，紧邻外部调用前标记 dispatched；重启后已完成的同一请求回放已保存的 `AIMessage`，不再发请求。已发出但无完整本地响应时，同一 call ID 禁止重发，结果与费用维持 unknown；确需另试须显式建新 call ID 并另占次数。标记 dispatched 与网络实际送达之间的崩溃窗口也保守算 unknown。提供方通常不保证以 call ID 查询或幂等，不能承诺 exactly-once。异常捕获后的原因只记录类别，不把异常文本中的凭据写进账本。

响应须有单一 assistant choice、消息身份及合法终止字段。invalid arguments 保存为 invalid_tool_calls；截断或终止原因不一致的工具不能交给动作执行。`AccountedProviderModel` 在保存响应和 usage 后，对整条 format_error 停止，不执行同一响应中其余合法工具，也不暗中请求修正。HTTP、超时、超限或无法解析的响应使用固定安全原因；已派发但未取得可保存消息的调用保持 unknown，不保存异常正文中的私有内容。

收到消息后保存规范化消息及哈希、提供方消息 ID、工具名称/ID/参数摘要与格式问题。wire 的非 null 原始 usage 完整保留为 response_metadata.dfx_provider_usage_raw；prompt_tokens/completion_tokens/total_tokens 须为非负整数、合计一致并与 usage_metadata 的 input/output/total 对应，才能标为 known。原值缺失/null 为 missing，存在但无效或映射不一致为 invalid；账本保存原值摘要，不以类型强制转换或文本长度补估。格式错误仍是完成的一次调用，不能丢掉其 usage。原始消息用于本机受管恢复，不进入 Git 或普通聚合报告；显式本机私有 Export 保留相关账本原字节并披露完整消息范围，不自动外发。text_only、模型自称成功和工具参数不产生执行事实。

无知识的新运行使用 session schema 10；显式批准知识的新运行使用 schema 11，增加规范 knowledge_profile，并在首个模型调用前完成第 3.1.2 节的 run 内原件复制。三态入口另使用 session 12，增加规范 applicability_profile；显式静态策略使用 session 13，另固定 static_skill_profile，要求批准知识及显式三态策略。未指定增强保持对应原代；各代均冻结 model_profile、context_profile（含工作上下文/主机压缩策略及两类提示指纹）、项目 system_prompt、初始任务消息、工具 schema/描述、检索限额，以及预算、能力、协议、策略、合成 registry 和下述执行来源；续跑须配置与执行兼容身份一致。知识运行的程序启动参数为 knowledge=HostKnowledgeInput，恢复仅接受原 TaskKnowledgeBinding 的显式 knowledge_approval，不能再次传入启动知识库；实际快照和索引从该 run 原件回读，外部库无需仍存在。无知识运行不能在恢复时取得知识批准；11、12、13 不能跨代续跑，三态和静态恢复须再次显式提供与原 profile 一致的策略。旧 session 8/9 只能按原契约只读核验，不自动升级、续跑或补写版本；schema 1–7 不进入当前恢复入口，另建 run 保留历史。Graph 仅保存 solver 响应，恢复核对连续 model-N 身份、solver 用途及已保存响应；LangChain 在持久化边界之后添加的消息 ID 不改变调用身份。摘要数量不作为 solver 序号，全部用途仍共享总预算。Graph 检查点之前的完成响应可按同一调用回放，已发出且未保存的响应停止为 unknown。已结束的 Graph 续跑只核对和汇总，不注入新对话；预算已耗尽仍明确报告。输出 Token 设置只是请求参数；max_known_tokens 达到后禁止新请求，不构成实际总 Token 硬限额。真实端点、密钥/数据外发边界、提供方兼容性、实际计费及应用接受门接入仍待完成。

execution_provenance 是嵌入 session 的 schema 1 主机记录，不是模型可提交对象。source_inventory 记录 src/dfx_agent 下全部 Python 源码及 scripts/dfx.py、local_agent.ps1、local.ps1、local_job.cs 的规范相对路径、大小和原字节 SHA-256；文档、测试、历史 session、数据和缓存不属于这份运行源码清单。repository_inputs 固定 uv.lock、pyproject.toml、.python-version、configs/local-runtime.json 的原字节大小与哈希。runtime 保存实际 Python 实现、版本、cache_tag、平台、机器架构和 importlib.metadata 读到的已安装 distribution 名称/版本，不把锁中声明的版本冒充实际安装版本。Git 保存初始 HEAD、全仓 dirty、状态字节摘要及条目数；不可用时明确为未知，不猜测干净状态。

source_inventory_sha256 固定源码清单；compatibility_sha256 固定清单、repository_inputs 和 runtime；provenance_sha256 固定包含初始 Git 观察的完整记录。读取时严格核对字段、类型、范围和三层摘要自洽性。Git 观察不进入兼容摘要，因此文档/测试修改或仅含文档的提交不会单独阻止续跑；执行源码未提交变更仍由原字节清单捕获。兼容的续跑保留原 session 和初始 Git 事实，不用当前 HEAD 或 dirty 覆写历史。清单只保存路径、大小和哈希，不自动复制主机源码内容、未提交 diff 或安装环境；它能支持同库漂移检测，不能独立重建尚未保全的主机修改。正式 W08 比较仍须固定并保全可重建的主机代码和环境材料。内容哈希不是签名，安装版本来自元数据，不提供第三方包二进制完整性或内存中全部已加载代码的证明。

标准 scripts/dfx.py 入口在导入 CLI 前建立受限源码基线，导入后复核；Agent 新运行在创建工作区前捕获来源，续跑先只读核验已有工作区/session 和当前兼容身份，再接入可写账本、Graph 或动作。知识续跑另核对显式原批准值、run 内 profile/完整原件、全部已保存查询及其模型/动作关联；已知查询漂移在可写账本启用前拒绝。缺账本时仍只读审查原件，已有查询不能以未知成本为由继续可写恢复或重新派发。CLI 对两种 resume 都先预检；mock 响应偏移只读 ModelCallStore，不创建表或修改 WAL。完整 session 配置也须在可写检查与账本启用前匹配。ResumeAdmissionRejected 使用 CLI 退出码 3，工作进程保留原终态；已获准执行后发生的失败仍保存 failed 终态及已登记事实。结束前再次核对兼容身份，途中漂移不生成混合版本的终止报告，已产生调用和成本保留。前后快照不是外部文件锁或内存执行认证；真实 HarmonyOS 工具链、运行工程与设备版本仍由 W08 独立绑定。

context_profile 定义有限上下文和可选主机压缩：默认 disabled 时，两种模型在 reserve 之前核对完整序列化消息的 UTF-8 字节上限；enabled 时先按 CompactionPolicy 触发并核验完整历史，system 与首次任务消息固定保留，assistant 多工具及全部对应结果为不可拆的一组。只压缩较早闭合前缀，保留近期完整组；孤立、重复、缺失结果或不支持结构在摘要调用前拒绝。Graph 原历史不删改，实际 solver 视图仍须满足 models.py 的硬限额；必须保留的尾部或摘要输入过大时明确停止。首版重新摘要完整前缀，不支持滚动摘要；策略参数唯一维护在 context_compaction.py，字节限额不代表提供方 Token 窗口。

每个 solver 位点最多一个稳定 compaction-model-N 调用，kind=context_compaction、tool schema 为空，独立 prepare/invoke，不绑定或更改 SDK solver 工具。输入为明确引用的数据，归纳提示要求保留失败、未知和来源。work/compactions/model-N.plan.json 冻结完整原消息、哈希、范围、策略和模型 profile；同位点输入改变则拒绝，不能以新内容哈希绕开未知调用。摘要与求解共用 ModelCallStore、次数、已知 Token 和截止时间，无 SDK 隐式重试或免费摘要。已完成摘要在 Graph 落盘前崩溃可回放；reserved 尚未发出可按原请求发送，dispatched/unknown 不重发。摘要用掉最后额度后停止，不额外执行 solver。

摘要响应先保存完整消息和 usage，再核验 text_only、非空 UTF-8、输出上限和实际缩短；工具调用、格式错误或不缩短的摘要以 context_summary_invalid 停止，不执行或自动修正。有效 result.json 保存带 authority=model_inference、范围与原消息哈希的 HumanMessage 包装及有效发送视图；solver.json 绑定其实际请求和账本。只读 audit 重建这些关联，拒绝篡改、错归属、链接、共享硬链接和孤立产物；摘要声明“通过”不生成检查、诊断确认或其他 host fact。摘要及派生调用不得作为 ToolActionStore 的工具授权父调用。SDK 的 SummarizationMiddleware 继续排除；必需的 FilesystemMiddleware 通过公开同类替换关闭消息与结果卸载。基础/增强使用相同配置，不把上下文能力差异算成领域方法收益。

`offline_script.py` 的受限 loader 接纳固定响应 schema 1/3 或 mock_chat_completions schema 2/4；新 3/4 必须显式提供 working_context，旧 1/2 拒绝该新增字段。可选 context_compaction.policy 接纳严格 CompactionPolicy；固定入口的 context_compaction.responses 是独立 text/usage 摘要列表，不消耗 solver 脚本。mock 入口禁止该独立列表，摘要和求解共用按实际请求排序的 wire 响应流。脚本必须是普通文件，路径链拒绝链接/重解析点，文件拒绝多硬链接；读取前后核对身份、大小及修改时间并冻结规范 JSON。字段、正数预算、策略和合成 expectation 严格校验，返回对象每次生成独立模型和响应流。CLI repair/resume 经现有受管离线入口分流；mock 续跑按全部已完成调用（包括摘要）偏移响应流，完成调用仍由账本回放，次数与时间额度不重置。脚本 expectation 仅供合成执行器，不能作为应用 oracle 或进入真实求解上下文；该入口不开放云端实验。

### 1.2.3 已登记诊断到修复决策

`plan_repair_action(plan_json=...)` 接收 `DecisionRequest`，由 `inspect_analysis(include_schema=True)` 提供 schema。请求绑定 task_id、run_id、diagnosis_id 和含源码版本的 PolicyInput；失败后再决策须成对传 candidate_id 与已保存的 feedback_id，不接受模型自填失败观察。旧的独立 PolicyInput 仍可供纯策略函数使用，已不作为 SDK 工具输入。

引用有限条件结果时须同时传 condition_result_ids 和完整 condition_scope；不允许仅凭结果 ID 猜测用途。主机重新核验已保存结果，要求同诊断、同分析版本、完全相同的候选/操作/场景/路径/对象/采样点，且结果对应的假设属于提议方向。若同时使用候选失败反馈，条件范围须属于该反馈候选。原版结果可用于原版范围下提出新修法，但不得作为新候选的条件结果；候选条件无需伪造 feedback_id，直接在 condition_scope 中指定候选。

每个 RepairDirection 包含 direction_id、hypothesis_ids、support_evidence_ids、obligation_ids、repair_key 和 acceptance_key。方向须引用本诊断的假设和证据，包含相关假设已关联的义务；义务须与方向假设或同包依据有关。检查的 outcome→方向映射须对应已登记 DiagnosticQuestion 的 outcome→假设映射；类型和能力须匹配。这里只核对引用，不能证明证据足以支持方向。repair_key/acceptance_key 相等是模型声明的修法/验收等价，未通过补丁或业务行为验证。

主机覆盖模型填写的可用能力和探针预算：离线路径仅保留主机实际提供的 read_source/read_log，执行探针预算为零。同修法且同验收直接建议候选；不同修法须有区分力、能力和预算才建议补查；否则允许带 unknown 尝试或 unresolved。建议本身不读取源码或执行探针，实际读改查仍经过共享工具授权与预算。

有效反馈必须来自同任务已冻结候选的已保存失败合成检查，匹配候选源码哈希、execution_id 和原观察。引用另一诊断时该诊断须明确 feedback_candidate_id。失败关联的义务必须已由原候选声明；scope=synthetic、evidence_kind=local_execution、link_authority=model_inference。只重新考虑相关方向，不硬拒绝推断义务，不把失败解释升级为已证实的因果关系；关闭增强时保持一般未知路径。

有限条件关联单独保存为 condition_review，不使用失败执行 Counterexample 的来源语义。开启 obligation_tracking 且引用结果为 violated 时，只建议重审同假设的方向；satisfied、unknown 或关闭关联时保留未知。原版条件被违反不等于某个修法已失败；算子/绑定适用性仍是推断，不自动确认根因、满足业务义务或硬拒绝候选。基础与增强均可计算、保存和回读相同条件，增强开关只控制关联建议。

`decisions.py` 保存不可改写的 DecisionRecord：规范化请求、诊断/义务/反馈/条件上下文哈希、策略开关、原始策略建议、反馈建议、condition_review、最终 next_action 及推断层级。身份摘要排除接纳时间等审计元数据，但保留语义输入；相同请求和上下文回读原结果。回读重核条件引用和建议，不接纳改写的关联方向或动作。inspect_analysis 可列出决策/反馈/条件 ID 并按 ID 回读；报告包含这些记录。保存决策属于 record_analysis，不冒充执行检查。

候选可选成对引用 decision_id/direction_id；主机核对同诊断、已提议方向和候选声明义务。决策是建议，允许模型选择其他已提议方向；未知建议本身不阻止合法候选登记。自动根因确认和真实应用反馈尚未接入。

### 1.2.4 任务指引与通用源码检索

`task_strategy.py` 的共享任务策略通过 Deep Agents 的 system_prompt 接入，启动消息只含主机核对的 task/run/source 身份、能力、续跑状态及 offline_synthetic/not_run 验证级别。基础指引覆盖日志→跨文件追踪上游和消费者→原始行假设→保行为补丁→冻结候选→实际检查反馈→有依据的修订或诚实终止；增强开关只追加对应规则、义务与决策检查指引。缺少有限条件输入不禁止基础修复；文本检索与模型说明均不能确认因果或真实运行值。

`search_source(query, path_prefix=None, candidate_id=None, case_sensitive=True, offset=0, limit=30)` 只接受有限单行字面查询；大小写忽略也是转义后的字面匹配，不执行用户正则。path_prefix 是相对文件/目录的组件前缀，不展开 glob；模型不能指定任意 root。原版整树摘要必须匹配任务，候选须已登记且冻结字节、manifest 和 diff 相符；草稿不参与检索。规模、单文件、查询、片段与分页限额由 source_search.py 的固定常量和 search_limits 定义，超限拒绝，不静默放宽。

结果包含原版 source_snapshot_sha256、实际 snapshot_sha256、scope、candidate_id、查询与过滤选项，以及 matches 中的 path、从 1 起的 line/column、有限 text、snippet_start_column、text_truncated、原字节 file_sha256。同一行只返回首个命中，按路径和行稳定排序；total_matched_lines、has_more、next_offset 明确分页，返回的下一页位置必须可用。NUL/非 UTF-8 文件以 skipped 原因列出；complete 只表示过滤范围内文件均可解码，不等于当前页包含全部命中，更不表示代码安全。长行片段截断用原列号定位，不能把片段当完整上下文。无匹配且 complete=False 时不得声明该范围没有目标文本。

检索属于 inspect_existing/read_source，基础与增强共享同一工具和总预算；实际 candidate_id 进入主机授权，不可用 proposal_json 隐藏。完整工具回执进入现有 SQLite，已保存结果恢复时只回放，不重复扫描。指引、工具和检索限额由第 1.2.2 节的当前会话固定；最终实际消息/工具/参数仍由每次 ModelCallIntent 留指纹。主机报告按第 1.3.2 节核验，不新增应用验证能力。

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

完整 Attempt/FinalReport 的应用级交付对象仍待接入，须保存诊断/候选/验证引用、下一步动作、累计和本次成本，并分别保留初次与最终根因预测。当前离线报告按下节从主机记录重建，不能用成功补丁落点覆盖初始定位成绩。

### 1.3.2 主机终止摘要与结果核验

`agent-report.json` 的无知识新运行使用 report schema 7/completion schema 4，与 session 10 配对；批准知识运行使用 report 8/completion 5，与 session 11 配对，并保存 knowledge_profile 和 knowledge_facts；显式三态策略运行用 report 9/completion 6 与 session 12 配对，另保存 applicability_profile 和 applicability_facts，关闭对照也使用同一代；显式静态启停使用 session/report/completion 13/10/7，另保存 static_skill_profile 和 static_skill_facts，完整读取与后续实际可见性分别认证。旧 session 8/report 5/completion 2 与 9/6/3 按原规则回读，禁止交叉配对或给旧记录补写来源/知识身份。`reporting.py` 的 build_completion 在运行结束后读取持久事实生成 host_summary，不采信模型结尾的“修好了”。它核对基准原字节、登记诊断/义务/决策/条件、候选源码/manifest/diff、检查意图与结果、模型账本和工具动作的归属/请求/回执，以及工作上下文冻结材料、可见动作、压缩原消息、摘要响应与实际 solver 视图的关联。知识报告还须显式取得实际 task_directory，从 session 冻结的 profile 核验 run 内批准原件，重算查询和完整指导，再核对成功工具回执与已记账 solver 响应、action_id、model_call_id、实际参数和回执全文；磁盘上存在查询不表示模型使用过它。knowledge_facts 分别保存查询及成功动作关联、回读关联、未关联查询/检索 ID，孤立产物保留为未使用。新 report/completion 的 execution_provenance 必须等于已验证的 session 来源；回读不捕获当前 Git、源码或安装环境来替换它。context_compactions 保留有效/无效、未请求/等待/未知、结果是否落盘及 solver 绑定状态，facts_sha256 固定包含保存来源和知识审查的输入，summary_sha256 固定派生摘要；哈希表示内容一致，不提升证据来源。

定位按工具动作账本排序：initial 是首次允许 edit_source 之前、已有完成回执的首个登记诊断；首次允许编辑即形成 cutoff，即使该编辑后来失败，也不能用后续诊断补成初次预测。latest 是最后一个有完成回执的登记诊断，candidate_linked 是最新候选实际关联的诊断；三者可以不同。已登记但没有可核对动作顺序的诊断列为 unsequenced，不猜测其先后。各定位保持 model_inference/root_cause_confirmation=unknown，包含候选根因位置及原证据；没有 ground truth 判断就不生成定位准确率。

候选按登记 ordinal 排序，保留全部 patch 引用及摘要、manifest、not_requested/pending/completed 检查状态和实际回执。latest_candidate_id 只指最后一个候选，最终状态依据它的检查，不选择历史上最好的一次：旧候选通过而最新候选失败或未查时，保留历史通过列表并报告当前 unresolved。未完成模型/工具/检查优先保持未知，模型格式或不支持请求同样 unresolved；预算耗尽按其停止条件报告。离线通过最多是 offline_checked；有已保存检查时 verification.level 为 offline_synthetic，否则为 not_run；accepted 始终 false，application_verification 始终 not_run。

cost 从模型和动作账本汇总所有登记调用、已知 Token、missing/invalid usage、未发出/结果未知调用、拒绝动作与检查请求，并以 calls_by_kind 区分求解与摘要等用途，分用途次数之和仍是统一总分母。只有全部登记调用完成且 usage 完整时 total_tokens 才有数值；否则保持 null，同时保留已知 subtotal 和未知计数。没有实测依据的 monetary_cost、localization_ms 和 repair_ms 保持 null，不能用脚本长度、合成响应或零值代替真实成本和耗时。

`read_completion` 只回读并重新核验，不运行模型、工具或检查。它核对 report 的归属/schema、整个 completion，以及 report_validation.verified_top_level_fields 列出的候选、检查、累计模型/工具成本、动作、决策、条件等顶层字段；新记录的 execution_provenance 纳入核验，policy_features 有会话依据时、offered_tools 有模型调用依据时才列为可核验。adapter_error_reason 与各 *_this_turn 成本差分当前属于 unverified_top_level_fields；stopped_reason 是重新计算的输入，部分原因须有账本依据，但不是完整重建的停止过程。调用成功不表示整个任意报告字段、应用结果或模型语义已被验证。

后续聚合只消费与持久事实匹配的摘要，并分别记录初次定位、最终定位、首次候选和最新候选，不删除失败或未知项；真实应用指标仍须共同接受协议与独立 oracle。受管导出须保留重新核验所需的明确产物、版本和来源；仅复制一份 agent-report.json 不能代替可复现证据。

### 1.3.3 停止记录的只读离线聚合

EvaluationManifest schema 1/2/3/4 固定 offline_synthetic 范围、唯一 evaluation_id、共同 RunConfiguration、任务、策略组、repeat 和完整格子。v1 保留原序列化形状；v2 共享 working_context 三个限制参数，组内必须显式声明 working_context_enabled，只有 enabled 可作为该代新增比较因素。v3 在 v2 上增加所有组共享的 knowledge_profile：明确 snapshot_sha256、完整 scope、retriever、index_sha256 及仅 knowledge/ 原件的 files 清单；task/run 和各运行的 approved binding 不进入共同身份。各 session 11 的投影必须与这份共同身份全文一致，普通检索和增强组共享完整指导及两个知识工具，不借知识机制放宽模型、工具、通用压缩、其余上下文参数、检索、能力或总预算。v4 继承 v3 的共同配置，variants 必须另声明 applicability_enabled，使用 session 12 及相同 19 项工具进行三态启停比较；仅该开关作为新增因素。v5 继承 v4，configuration 另固定 shared_static_skill_profile，variants 另声明 static_skills_enabled，session 13 两组均提供相同 20 项工具和完整正文，static 共同身份仅去 enabled，正文、元数据、资源、工具及预算不能随组变化；学习因素尚不接入。v4 不接纳静态运行或残留，v3 不接纳三态运行，无知识旧清单不接纳知识运行；即使缺 session/report 也拒绝不兼容领域目录。任务绑定 task.json 原字节哈希、源码/日志、协议与主机输入摘要；task schema 2 必须提供哈希一致的 CaseSelection，task schema 1 的案例和谱系仅为声明。格子绑定 task/variant/repeat、唯一 run_id 与可空的工作目录。全部矩阵恰有一格，拒绝重复 run、任务/日志身份及祖先/后代目录重叠；格式容量在矩阵展开前检查，不选择性发现成功目录或根据结果删格子。

既有工作必须有匹配的 schema 1 offline 终态 finished/failed/canceled。目录缺失或终态无报告列 missing，work_path=null 列 not_run，费用未知且保留分母；现有损坏记录拒绝整批。核验 session 8、9、10、11、12 或 13 的任务、协议、主机输入、共同预算/能力/模型/上下文/工具/检索配置、策略开关和重新生成的提示；只构造主机工具描述，不执行工具，核对 session 与实际 solver 的两种工具格式。随后使用 read_completion 核对对应的 report/completion。session 9/10/11/12/13 的来源按保存结构及摘要核验并传入行记录，不要求等于回读主机。终态缺报告仍须核验已有 session 的配置和来源；session 11/12/13 还须从实际 task/work 原件回建批准知识并审读全部查询。模型/动作账本完整时核验调用关联，账本缺失时仍审读原件而不认证工具使用，费用与修复结果继续未知。旧 solver 原始请求体未保存，不能声称重建全部历史请求参数；session 10/11/12/13 开启工作上下文时，其各位点计划和发送回执单独核验；session 8 未记录的执行版本继续 null。

只有旧来源的矩阵保持原 schema 1 汇总结构与内容，不改变既有导出包的 aggregate 摘要。存在新来源时，按保存的兼容身份及完整来源摘要列出 captured_records、关联 run_ids、captured_runs 和 unknown_runs/unknown_run_ids；旧记录、未运行和缺来源格子继续未知，不将它们删除或补零。所有已捕获来源的 compatibility_sha256 必须一致，不同初始 Git 观察可分别保留；已知执行源码、锁或实际环境漂移拒绝整批比较。来源覆盖并不产生应用成功资格。

ModelCallStore、ToolActionStore 和 CheckLedger 显式 read_only=True：不创建文件/目录、表或 sidecar，写方法及执行入口一律拒绝。ReplayOnlyExecutor 只持有协议摘要，check 始终拒绝；不读取预期答案。SQLite 以只读不可变连接回读，非空 WAL/journal 拒绝，不自动合并。聚合检查原路径链、链接/硬链、完整源树字节和文件身份，前后变化拒绝；主机 JSON 严格检查，源码 JSON 仅校验原字节。停止标记和前后检查不替代对外部并发写入者的锁。

汇总保留全部定位、候选、失败、压缩与成本，缺项不补零；成对 unknown 不作失败收益，repeat 与任务/谱系分别计数。应用指标全部 null，accepted 始终 false，具体分母和成对资格以 [评测口径](preparation/metrics-and-gates.md#当前离线聚合的口径) 为准。受管 aggregate 只写新的工作目录，不能覆盖或写入输入树，打印有限回执并保存终态；强制中断未写终态时保留该未知目录并阻止自动清理。本机私有证据 Export 按下节保存同一矩阵及回读依据，不改变应用接受语义。

### 1.3.4 停止记录的本机私有证据导出

W10 的离线 Export 消费 EvaluationManifest schema 1/2/3/4；当前实现的合成验收结果由 STATUS 维护。目标是完整保留既有记录并能从包内重建同一离线摘要，不执行模型、工具、检查或 Graph 反序列化，不删除源输入。公开脱敏、真实应用汇总及正式提交仍是独立的后续工作。

| 对象 | 必须字段与绑定 |
|---|---|
| ExportPlan schema 1 | offline_synthetic、export_id、owner_id、manifest 相对路径及原字节摘要、aggregate 摘要、owner/config 摘要、隐私说明、source_snapshot、inventory、estimated_bytes、plan_id；plan_id 固定其余规范内容 |
| source_snapshot | 每个显式 task/work 相对树的文件大小、身份、修改时刻和字节哈希，包括空目录；不存在的 work 明确为 null，不从目录发现结果或删掉缺失格子 |
| inventory | 包内规范相对路径与 directory/file 类别，文件的 size/sha256；拒绝重复及大小写碰撞、链接、共享文件、额外/缺失节点和输入/目标祖先重叠 |
| package.json | schema/scope/status、export/owner/plan 身份、manifest/aggregate 摘要、privacy、完整 plan、导出器模块/schema 来源与 package_sha256；内容哈希不是签名，也不升级来源可信度 |
| 导出/Verify 回执 | 有限的身份、状态、包摘要和预计字节、隐私说明；控制台不打印原消息、完整清单或私有 schema 输入，完整记录留在本机包内 |

预览核对当前仓库与 owner、资源配置、无其他活动/历史未知作业、任务快照和匹配的 offline 终态。拒绝原路径链链接、硬链接/共享文件、未知顶层条目、凭据类文件、版本/协议错配，以及任何需要恢复的非空 SQLite WAL/journal。只读检查包括 Graph checkpoints.sqlite 的数据库完整性，不加载其中的序列化消息。保存位置固定为 state/export-plan-ID.json；可刷新通过校验的同 ID 旧计划，未知已有文件、manifest 及其 .new 路径重叠均拒绝，不能将保存计划变成覆写输入的入口。

Apply 重新从原 manifest 派生范围并复核完整 PlanId，不直接使用可编辑计划中的任意路径。受管 tmp 的独立短 ex- 前缀目录是未发布暂存；它与所有输入必须分离。payload 镜像原 task_path/work_path，保存任务日志/源码和现有 work 的完整基准、草稿、全部候选与补丁、Graph/模型/动作账本、检查、诊断、义务、决策、条件、采值和压缩产物。知识运行另保留 knowledge-profile.json、knowledge/、experience-bindings/ 和 experience-queries/；批准快照、来源材料、条目、完整索引及该 run 检索记录均在包内，不引用外部库。knowledge/indexes/*.sqlite 额外核验只读数据库完整性，包内聚合仍重核原件和全部派生行，不执行索引重建。显式三态运行另保留 solver-inputs/、applicability-contexts/、applicability-materials/ 和 applicability-reviews/，只读重聚合核验实际发送、冻结材料与动作关联；Skill 的导出接入仍待后续契约。终态缺报告的目录仍保留全部现有字节；不存在的 work 不建空壳，not_run/missing 与未知费用保留在原矩阵中。

evaluation.json 是原 manifest 字节，aggregate-report.json 是源端重算的完整摘要。复制按 inventory 校验每个文件及空目录；清单和汇总在写入前再次匹配已预留的大小及哈希，拒绝复制期间增长后再超额写入。前后复查源树、owner/config 和缺失状态；包内重聚合必须与源端全文及报告摘要一致。容量满足后仅发布新的 runs/exports/ID，目标出现或输入漂移即拒绝；失败暂存未发布且源输入保留。Windows 长路径处理仅在项目接口内进行，不改系统配置。

Verify 的证据输入只依赖包内字节：重新核对 package/plan、清单、全部现有证据树及只读 SQLite，再重聚合并复查包前后不变。session/report 已嵌入的新执行来源随原字节保存，无需升级 package schema 或添加来源 sidecar；回读只验证保存来源的结构和哈希，不将其与当前 Git、运行源码或环境兼容身份强制匹配。受管工作进程仍须核验当前环境的 owner、依赖锁与自身租约；包中 owner_id 仅记录导出来源实例，经过格式检查后作为预期包来源交给核验器，不要求等于当前运行环境实例。Clean all 后重新 Prepare/Init，在支持对应历史契约的项目实现与受管环境中仍可核验保留的旧包；不改写包、原输入或旧运行记录，仅产生当前受管日志与租约。来源 ID 和内容哈希不认证制作者、不授权接管或清理旧资源，也不证明 Graph 恢复、摘要忠实度、真实因果、业务功能或应用修复效果。历史 session 未记录的 Git/lock/SDK 执行版本继续 null，不以当前导出器版本补写。

privacy 明确 local_only=true、redacted=false、share_ready=false，并披露源码/候选、故障日志、原模型消息、工具参数/结果、Graph、失败尝试和成本。名称守卫拒绝凭据类材料不代表已证明内容没有秘密；当前包不得作为可公开材料或自动外发。单包/共同容量和清理保护由第 5 节约束，Export 不证明全部清理目标已保全，也不自动授权删除源证据。

## 2. 控制器转换规格

控制器只依据可校验记录转换状态。先验证原始版本能按目标协议触发故障；复现入口尚缺失时，允许继续离线诊断，但运行修复任务不能成功终止。

| 当前事件 | 必须保留的信息 | 允许动作 | 禁止推断 |
|---|---|---|---|
| 原始日志映射有歧义 | 全部候选路径、版本未知 | 补查源码和打包来源，或保留未知 | 随机选第一个文件当成精确定位 |
| 两个诊断解释会导向不同补丁 | 区分二者所需事实 | 预算内补查或执行一个探针 | 多做一次相同检查算获得新证据 |
| mock wire 模型响应有格式错误或不支持的请求格式 | 已有消息、原始 usage/安全原因和调用状态 | 保存后 unresolved，或在明确新调用预算下另行处理 | 执行同轮部分工具或在 SDK 内暗中重试 |
| 模型已派发但响应未保存 | 请求身份、账本状态和未知费用 | 回读已有结果；否则 unresolved | 没有结果就假设调用未发生并重发同 ID |
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

工作上下文、跨任务经验与 Skill 演进流程见 [设计第 7 节](development/system-design-draft.md#7-skills-的具体位置)，对象和行为契约在第 3.1 节定义。主机批准经验、冻结快照、确定性检索及独立三态接口已有实现；普通检索及显式三态 SDK 切片覆盖 run 内原件、工具、会话、报告、聚合与私有导出，受管验收结果见 STATUS。静态过程资源、SDK middleware 与实际 Agent 会话交接已有实现，受管配置使用显式分代的固定/模拟脚本，自动学习仍待完成；规则卡不能等同于这些能力。独立契约、实际发送和后续学习分别验收，只有明确的新 schema 接纳对应因素，具体依赖与反例见 W07 实施顺序。

义务记录由模型工具创建时固定为 `model_inference`，须引用已登记诊断证据；候选的 restores/preserves 只能引用本次运行、本源码版本已登记的义务。合成检查实际失败后，模型可把失败与候选义务关联为 `scope=synthetic`、`link_authority=model_inference` 的反例；观察文本来自执行器，违反哪项义务仍是模型解释，不构成硬拒绝或应用行为证据。反例只关联原候选哈希及源码版本。可信需求、独立业务判据和应用反馈接入后，才能提高证据层级。

### 3.0 首批有限条件检查

`conditions.py` 提供独立程序接口及两种离线模型共用的 evaluate_condition 工具。它回答“这组有界输入是否满足指定条件”，不回答“源码一定产生这些值”或“业务已经修好”。不解析或求值 RuntimeConstraint.condition、ValueBinding.expression；只使用已登记绑定 ID 和有限枚举算子，不作为 CheckLedger 执行器。

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

evaluate_condition 只收序列化 request_json、可选 observation_id 和动作提案；不接受模型自填值、fixture 或结果。主机可通过程序接口注入合成 registry；当前 CLI 默认空 registry，未提供 ID 时保存 missing_observations 的 unknown，未知 ID 拒绝。inspect_analysis 可列出输入 ID/范围和结果 ID，按 condition_result_id 回读，并按需返回请求 schema。计算登记属于 record_analysis，引用与候选纳入相同工具授权和总工具预算，不消耗一个虚构的执行探针额度。registry 指纹由第 1.2.2 节的会话固定；恢复不能替换输入，已保存工具回执则回放，不重复登记动作，回执未存时保持动作结果未知。condition_results 进入第 1.3.2 节的主机报告和核验范围，来源仍为合成。可信值采集、目标版本适用性和真实应用验证尚待实现。

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

当前独立采值合成入口不升级既有 Contract/schema 1 或 ConditionStore，也不改变第 1.2.2、1.3.2 节的会话/报告语义，不开放新 SDK 工具、真实来源或运行能力。既有 synthetic/unknown 路径与测试继续有效，既有 applicability_authority=model_inference 保留；新来源不能靠给旧结果增加一个“可信” sidecar 接入。

独立绑定/采值凭据契约与合成接纳门单独回读，不直接注入现有 ObservationBatch。真正接入新来源时先显式设计独立来源对象的新 schema；改变计算/SDK 工具语义时再设计 ConditionRequest/Inputs/Result/Record schema 2 和对应新 session/report schema，冻结新增 manifest。新会话不跨旧工具 schema 续跑，旧记录仍按旧规则回读为历史合成结果。旧 fixture 若引用新绑定，须登记新的请求并重新计算新 ID，来源仍 synthetic。禁止覆盖旧结果、直接修改 v1 封闭来源枚举或让 v1 默认接纳真实执行标签。

根因假设仍由证据和因果检验支持；可信局部采样也不自动改变 Diagnosis/RepairObligation 状态。ScopedConditionResult 不是 VerificationVerdict，不进入应用接受门充当 pass。共同协议的原故障复现、构建/目标/业务/回归检查仍由独立验收和实际运行提供。

### 3.1 工作上下文、经验与 Skill 的实施契约

本节定义 W07 新机制的输入、行为与拒绝边界，分步实施。工作上下文已有模型发送路径；经验准入、知识快照、确定性检索，以及绑定当前任务的冻结上下文和三态适用性交接已有主机接口。普通检索和显式三态 SDK 切片均已编码：主机批准固定知识、run 内复制原件、授权查询/回读，三态入口另核验实际发送的焦点与材料；对应会话、报告、聚合和私有导出保留完整记录。受管验收结论由 STATUS 维护，静态过程资源及 Agent 会话交接已有程序入口，受管配置已接入固定/模拟脚本，学习尚待接入。通用修复路径继续可用；来源/引用核验、确定性记录和合成分支不能证明模型推理正确、经验可迁移或修复效果。模型提出语义判断，主机核验已登记事实及权限，不把结构完整性当语义确认。

#### 3.1.1 工作上下文和当前焦点

先从已有不可改写领域记录构建结构化上下文，再接入实际模型消息。首包接口为 `build_working_context(workspace, focus, task_directory, ledger, max_bytes)` 和 `verify_working_context(packet, workspace, task_directory, ledger, max_bytes)`；只读核验原版、冻结候选、诊断、义务、决策和检查，不直接修改 Graph 或发送请求。

| 对象 | 必需内容与不变量 |
|---|---|
| WorkingContextFocus | 显式 diagnosis_id、hypothesis_ids、current_candidate_id、decision_id、feedback_decision_ids；可为空以表达尚无诊断。主机传入引用，构建器核对归属和关联；不凭文件排序、时间戳或模型“已修好”选择最新焦点 |
| WorkingContextPacket | schema/选择器身份、task/run/分析源码、完整 focus、原始记录及引用闭包、当前候选与历史候选的独立范围、unknowns、规范 payload 字符串及 SHA-256；读取返回副本，原字符串是内容事实，嵌套列表不可通过返回值改变 |
| WorkingContextPlan | solver 位点、动作水位及依据 action_ids、原有序消息、完整审计 packet、材料引用清单、求解投影、必须/选入/省略引用及理由、完整工具组区间、组织视图、选择 profile；发送回执另绑定最终视图、可选压缩及实际模型请求 |

闭包从焦点诊断、明确假设、候选恢复/保留义务和决策方向出发，加入支持/反驳证据、绑定/条件/因果步骤依据、区分问题及尚未解决项。首包保守保留选中诊断的完整 alternatives、问题与证据，hypothesis_ids 只是焦点提示，不删除反向依据。义务、假设和决策可以相互引用，按 visited 集合求固定点；证据 basis 派生环按既有约束拒绝。回读重查既有登记的全局 target namespace，诊断/假设/义务与 Evidence ID 不得重名；结构义务回到其唯一原诊断，不按文件顺序猜 owner。无法找到引用或版本不符时拒绝构建，不能生成删减后的“有效”包。

相关失败显式由 feedback_decision_ids 引入，重新核对原失败候选、源码、检查结果和关联义务；原候选 A 的反馈可以影响当前 B 的决定，但内容始终标记 A，不能说 B 已失败。首包仅接纳与当前焦点共享已登记义务 ID 的历史关联，仍标记 semantic_relevance=unknown，不把引用交集当语义确认；其他关系须后续显式提案核验，不自动推断。如果焦点决策本身依赖反馈或有限条件，则必须一并保留原范围与结果。当前候选属于焦点诊断时直接匹配；否则新诊断须以 feedback_candidate_id 明确指向已登记的 previous，并有该诊断内已核验的同 previous/执行快照失败 synthetic local_execution。主机可以将该 previous 或与其同原诊断、同 task/run/分析源码的已登记修订候选选为当前焦点，但 previous 的失败观察仍为旧候选范围，不能因此充当修订候选的当前证据；仅声明 ID 或其他不匹配拒绝。旧反例不会因年龄而自动省略。

首版核验 source_fact 的原版行片段和摘要、输入日志原字节/引用内容、推断声明的摘要及完整依据、合成执行的注册候选与已保存检查；来源字段原样保留。推断和模型义务保持 unknown/model_inference，不能从磁盘改标获得可信资格。无哈希的历史草稿读取不纳入当前结构化事实；主机焦点由调用方显式提供，首包不认证它是已完成 SDK 动作的最新选择。

必要包的规范 UTF-8 字节数大于调用方明确预算，返回 `working_context_required_limit`，不截断义务、证据或来源。该数值仅是 packet 预算，不能当提供方 Token 或完整 wire 请求大小。载入对象须与受核验原字节绑定，多次读取间改变也拒绝，不能混用 A 版对象与 B 版来源摘要。回读用保存 focus、选择器身份和来源重建包，再比较规范内容及摘要；追加无关有效记录不改变旧包，相关记录变化或篡改须拒绝。检查从未请求合法进展为完成，也会使旧 packet 的同源点重建拒绝，需生成新 packet；它不是历史 solver 计划恢复器。首包不自动写回旧会话或添加文件持久器。

`working_context_plan.py` 的 WorkingContextStore 在模型边界组织上下文；WorkingContextPolicy 默认关闭，字节预算与近期组数在该对象定义并由 session 冻结。焦点只取当前输入中已完成、授权、有有效回执的工具动作，按可见顺序推导并保留水位。packet 中诊断、候选、检查、条件、决策和反馈也须有可见来源，磁盘上已经完成但当前消息尚未见到的检查不能提前进入包。没有诊断的候选和未纳入闭包的领域结果保持通用路径，相关完整工具组另作为必要材料。

处理顺序是原 Graph 历史 → 完整 packet 与材料核验 → 有界求解投影和必要组组织 → 可选通用压缩 → 实际发送。投影与尚未由它表达的必要完整组作为引用数据附入原 startup HumanMessage，保留原任务文本和其他字段；覆盖判断使用实际投影全文，不能只凭记录 ID 丢掉原文。通用压缩保留这段 startup，不能把必要原文替换成摘要。原历史的 assistant 多工具调用与全部回执是一组，孤立、重复或未完成组拒绝。startup、必要投影、必要组及近期组先占组织视图预算，剩余历史从近到远装入，最终按原顺序发送；超过必需额度明确停止，不截断义务。只有模型发送视图改变，Graph 原历史保留。通用压缩另外受自身输入/摘要限额及同一调用、Token、时间账本约束；消息字节不等于提供方 Token 窗口，真实窗口仍待接入核验。

同 solver 位点只保存一个不可改写计划；已 reserve/dispatched/unknown 的调用不能借新焦点重发。计划及发送回执保存于受管工作区 working-context，物化当时来源、focus、记录、profile、原消息和组织/最终视图。固定入口保存实际消息、工具描述和 stop/generate/bind 参数，模拟 wire 入口保存 PreparedRequest，重算消息/工具/参数摘要并与 ModelCallIntent 账本核对；组合压缩还核对组织视图是其原输入。历史恢复核验这些已保存材料与原模型/动作回执，不从最新检查重建旧 packet；正常后续检查不改变过去输入，缺失或改写历史材料拒绝。保留选择耗时、已省略组及未派发计划，不伪报零成本或收益。

无知识新写 session/report/completion 为 10/7/4，批准知识普通运行为 11/8/5，显式三态策略启停两组为 12/9/6，显式静态启停为 13/10/7；知识身份和共同评测门见第 3.1.2 节及第 1.3 节。context_profile 明确冻结 working_context_format=2。历史 session 10 缺该标记时只读按 format 1 核验原 inline 计划及原序列化形状，不倒填；旧 8/5/2 与 9/6/3 也维持原形状只读。执行来源不兼容的旧运行不能续跑。旧固定/模拟脚本 1/2 不接纳新工作上下文字段，新 3/4 明确登记 working_context；聚合清单 v1 仍可核验关闭增强的新运行，v2/v3/v4/v5 可显式比较 working_context_enabled，format 与其他共同配置门不放宽。报告审计和私有导出保留实际发送计划；这些能力不证明摘要忠实度、模型定位或修复收益。

format 2 的 plan schema 2 将原字节放在同 run 的 working-context/materials/<sha256>.blob，每个不可改写计划固定引用路径、大小与摘要。同字节材料在该 run 内复用；不跨 run 链接、不改写原文件。单次入库批次先核验全部逻辑引用、容量和同摘要原字节一致性，再按唯一原字节发布，所有逻辑引用保持；下一次独立操作仍重新读取已有 blob。批次不是整体写入事务，发布失败留下的孤立材料按下文处理。原材料、完整审计 packet、引用清单及计划各自沿用 `context_compaction.MAX_ARTIFACT_BYTES` 单产物上限；整体仍受既有受管磁盘及导出预算约束。WorkingContextPolicy.max_packet_bytes 在 format 2 专指模型投影预算，format 1 保持原 packet 预算解释，两代通过冻结标记明确区分。限额失败在 solver reserve 前停止，不提高配置自动重试。

首版求解投影只把完整 sources 回执清单替换为数量、逻辑字节数及清单摘要，并绑定完整 packet 的摘要；其余假设、alternatives、证据陈述、业务义务、反例、条件、候选范围、权限、版本和未知项全部原样保留，不做模型语义压缩，也不额外读取来源。Pydantic 仅核验投影/清单结构；WorkingContextStore 的回读、发送绑定及审计还须核验外部原字节、基线/候选身份、完整闭包及可见回执。不能把单独结构校验称为材料已认证。

规模优化仅复用同一次主机构建刚核验的输入和紧邻重复步骤：公共工作区 reopen 每次完整核验任务；内部延续入口不作为模型/工具 API，也不跨调用或恢复缓存。候选仍经检查账本核验，完成 packet 前独立复查候选、任务、基线及保留原字节。材料路径采用组件级包含和完整未解析祖先检查，拒绝父层链接/重解析点、共享文件及非正规文件。材料库只有受控直接子文件，批量读在起止核验完整祖先链；祖先绑定目录身份、类型与属性，允许材料库之外的无关兄弟文件创建，材料目录本身仍绑定完整身份。每个打开文件限于已捕获的原身份，保留读取前后路径/文件描述符、长度、原摘要及最后成员复核；读取量按实查大小加一个增长哨兵有界申请，不为小文件反复申请整个单产物上限。单次整库检查可同时返回已核验原字节供该次回执校验使用。下一次操作重读，不通过 size/mtime 缓存取代原字节，不宣称文件锁或原子并发沙箱。

先发布材料，再发布计划，再 reserve/link 模型调用。单 writer 受管运行中，崩溃可留下完整但未引用的 blob 或有界 pending 文件；它们只读保留并计数，不转成任务事实、不自动删除或填补缺失引用。namespace 拒绝非法文件名、目录、链接、超限及审计期间变化。报告同时保留逻辑引用字节、按摘要去重的引用字节、投影字节和整个 run 的 namespace 观察；后者在各计划重复展示，不求和为每调用成本。历史回读、未知调用不重发、组合压缩和私有导出继续核验完整材料。规模证据见 STATUS；材料去重属于工程基础，不能单凭字节减少宣称真实 Token 或修复效果收益。

#### 3.1.2 经验、检索和适用性交接

经验机制让 Agent 在新任务中获得可查证的历史指导：保留适用前提、未知查证问题、过程、保留行为和反例，再用当前证据判断能否借用。历史经验不作为当前根因或补丁已通过的依据。`knowledge_contracts.py`、`knowledge.py`、`retrieval_contracts.py` 和 `knowledge_retrieval.py` 提供材料准入、不可变经验、知识快照及确定性检索；`applicability_contracts.py` 和 `applicability.py` 提供当前任务绑定、冻结上下文和独立三态交接。`knowledge_run.py` 把主机批准知识复制成 run 内自包含原件，`knowledge_handoff.py` 提供普通检索 SDK 工具及只读查询/动作审查；`solver_visibility.py` 保存和审读实际发送输入，`applicability_handoff.py` 将三态准备、提交与回读接入 SDK。受管验收结果见 STATUS；静态过程资源及实际会话交接已有程序入口，受管配置和自动学习仍待接入。

知识基础路径是**主机审核完整可见投影 → 捕获获准材料原字节 → 按来源权限准入 → 物化经验条目 → 固定条目与检索配置快照 → 显式建立派生索引**。普通检索接入继而批准实际 task/run、复制完整原件到 run、授权有界查询并回读完整返回页；独立三态接口在绑定查询之后另冻结显式焦点及当前原件，再逐前提核验引用并保存建议。主机须先审核整个 SolverProjection 文档，为其原字节设定 MaterialGrant；ExperienceDraft 只引用已批准的材料 ID，不能提交任意新指导文本或自填权限、验证资格与成本。结构校验与主机角色声明不能识别自由文本中暗含的答案，审核完整投影仍是主机责任，不宣称已有语义防泄漏能力。

程序主机首次启动传入 `knowledge=HostKnowledgeInput(retriever, approval)`；approval 为 TaskKnowledgeBinding，实际 workspace/task/日志身份须一致。`prepare_run_knowledge` 只复制该批准快照精确引用的材料、条目、snapshot、索引描述符和 SQLite 原字节，不复制外部库未选条目或旧 retrievals。全部原件进入 run 的 knowledge/，批准绑定保存为 experience-bindings/approved.json。knowledge-profile.json 与 session 中的规范 profile 固定 schema 1、完整 approval、index_sha256、实际 RetrieverSpec、排序的 workspace 相对原件路径/大小/摘要及 profile_sha256，不含绝对外部路径。`read_run_knowledge` 以调用方提供的冻结 session profile 为授权，严格对比本地 profile、实际 owner/binding、完整原件清单和索引全部行，纯读回建 run 内 ApplicabilityStore；保存对象自报的范围不能扩大批准。

复制的首个写入前，先核定全部原件、批准绑定与完整 profile 的闭包容量，复用 ApplicabilityStore 的真实 owner 读取与知识合计容量/关闭门，并预检复制后来源二次 finish 和独立本地完整审计的字节/次数。复制前后均重新关闭来源核验，发布后独立回读本地原件；缺失、链接/共享文件、完整祖先漂移、同长度恢复 mtime 的改写、sidecar、超限或清单不完整均拒绝，不补索引、不修复缺项、不自动清理部分产物。各阶段仍按既有独立读取限额计数，不能把它们称为一次覆盖全流程的全局 I/O 门。未提供知识批准的运行启动/回读不创建知识文件；None 配置遇到知识、profile、绑定、查询或三态原件残留时拒绝降级。普通批准知识代也拒绝 solver-inputs/ 及三态原件残留，不能隐式取得三态能力。

模型只使用 `search_experience(tags, text, offset, limit, proposal_json)` 和 `read_experience(query_id, entry_id, proposal_json)`。search 返回 bound query 的 query_id、完整绑定及实际命中页；read 不带 entry_id 时回读同页，指定 entry_id 时只允许返回该页真实成员的完整 SolverProjection。参数不接受库路径、snapshot、scope 或主机权限。两个工具映射为 inspect_existing/read_source，经原 ToolAuthorizationMiddleware、模型响应身份和总工具预算授权；不新增免费调用或独立预算。查询、回执与已保存 solver 响应和动作关联重算后才认定使用，孤立 query/retrieval 只保留为未使用产物；缺账本仍审读原件，不能认证使用顺序或填零成本。会话/报告配对、共同知识身份及导出义务分别见第 1.2.2、1.3.2–1.3.4 节。

| 已实现对象/入口 | 内容及行为义务 |
|---|---|
| MaterialGrant / HostMaterialManifest | 主机声明 material_id、规范相对路径、原字节大小/摘要、role、origin_split、visibility、allowed_uses/allowed_splits 及可选提炼调用/已知 Token。清单拒绝重复 ID 和大小写路径碰撞；可以如实登记禁止材料，但登记不代表可入库或可求解 |
| HostMaterialRegistry | 从显式清单捕获原文件字节，核对大小、摘要及完整路径；不扫描目录、不根据材料自带指令猜 role。捕获后原文件变化不改变旧 registry，使用新内容须建立新清单/registry；后续入库复制捕获字节，不依赖原输入文件仍存在 |
| ExperienceDraft / admit_entry | draft 仅有 projection_material_id 和有序 history_material_ids。主机准入要求恰好一条 projection，其他来源只能是 synthetic_history；全部来源先通过学习用途、来源分区和可见性检查，再构造并发布条目 |
| SolverProjection / ExperienceContent | 完整指导包含标签、触发说明、带唯一 premise_id 的非空前提及必需 unknown_question、非空过程、观察分支、保留行为、反例与退出条件。标签可为空表达未知；不得携带 task/run、完整旧轨迹、隐藏测试/标签、参考补丁或案例到答案映射字段。epistemic_status 固定 historical_guidance_not_current_evidence，derivation_authority 由对应主机 projection role 约束 |
| HistoricalSource / LimitedValidationScope | history 原文只接受主机定义的 synthetic_fixture 描述：历史 task/run/lineage_group_id、源码及成对候选身份、报告/产物摘要、有限验证范围与 unknown/passed/failed、oracle 和环境说明。报告/产物摘要仅是该 fixture 的声明字段，不认证实际报告、执行器或应用结果；投影的 validation_scopes 必须逐项等于有序 history 的范围，不由模型重新总结成更强结论 |
| ExperiencePayload / ExperienceEntry | 保存完整投影、全部原来源 grant、有序 history 及主机赋予的提炼调用/成本。application_qualification 恒为 none；已知 Token 须有 derivation_call_id 且仅标 host_declaration，未知成本保留 null/unknown，不等同于实际 ModelCallStore usage。entry_id 为 experience- 加完整 payload 的 content_sha256；规范字符串是内容事实，读取 contents() 返回独立副本 |
| PermissionScope / RetrieverSpec | scope 固定 solve 用途、目标 split 和 excluded_lineage_groups；主机检索请求的整个 scope 必须与快照相等。检索配置冻结 tags_text_v1、标签/文本权重、返回及查询限额、entry_id tie-break 和 unknown_tag_policy=keep；用于确定性排序，不执行三态判断 |
| SnapshotPayload / KnowledgeSnapshot | 显式选择条目，拒绝重复并按 entry_id 升序固定各 entry/content/projection 摘要；scope、完整检索配置与 skill_files 一并参与 snapshot_sha256。当前 skill_files 强制为空，不接受声称已加载的 Skill 版本。快照引用已物化的完整来源闭包；进入 SDK 前须按批准范围复制为 run 内原件，私有导出从该 run 保存完整闭包 |
| MemoryQuery | 必需 snapshot_sha256 和 PermissionScope，保存输入标签原文、有限 text、offset 与可空 limit。匹配标签按 strip().casefold() 规范化，规范后空白、过长或重复拒绝；字符数按 text 加原标签原文累计，不能通过 trim 掩盖输入容量。空查询可浏览全部成员，未知标签不按词表丢弃 |
| IndexDescriptor / build_index | 固定 snapshot、SQLite 原字节摘要/大小、成员数、完整派生 rows_sha256 及 retriever_sha256。描述符自身规范内容摘要作为 index_sha256。KnowledgeRetriever.build_index 由主机显式发布固定快照的索引；描述符和库文件均不可改写，已有部分文件、缺失或漂移不自动修复 |
| RetrievalHit / RetrievalPayload | 每项保存 entry/content/projection 摘要、完整 SolverProjection、score 及升序唯一 matched_tags/matched_terms；payload 保存完整查询、描述符摘要、冻结检索配置、全体/正分成员数、完整返回页、下一 offset 及容量。结构校验核对身份、投影全文摘要、分页、排序、匹配查询子集与得分；真实匹配和完整成员还须由运行时重算。ranking_status 恒为 relevance_not_applicability |
| RetrievalRecord / query / record / read_record | record_id 为 retrieval- 加完整规范 payload 的 content_sha256；contents() 返回副本。query 纯读取并返回记录对象，record 才显式持久化；read_record 重新核验来源与索引、重执行保存的查询并比对完整记录，不只检查自报摘要。KnowledgeRetriever.read_projection 先核验记录，只返回该记录实际返回页中的指导副本 |
| TaskKnowledgeBinding / ApplicabilityStore.bind | 外部主机 approval 固定 task_id、run_id、分析 source_snapshot_sha256、crash_sha256、知识 snapshot_sha256 和完整 PermissionScope。bind 核验实际工作区、任务、日志及快照范围后保存 approved.json；每次读写都对照外部批准值，保存对象自报的身份不能扩大权限，同一运行不能替换批准内容 |
| BoundQueryPayload / BoundQuery / record_query / read_query | payload 固定 binding_sha256、retrieval_id 和 retrieval_sha256；record_id 为 bound-query- 加规范内容摘要。record_query 仅接受 tags/text/offset/limit，快照与 scope 由主机填入；read_query 重算原检索记录并核对外部批准范围，不接纳其他任务绑定或未经绑定的检索记录 |
| ContextMaterial / ContextPayload | 每项固定 task/ 或 workspace/ 逻辑引用、原字节 sha256 与 bytes；引用须规范、排序、大小写唯一，拒绝越界、末尾点/空格及平台歧义路径。payload 固定 binding_sha256、bound_query_id、完整 WorkingContextPacket 及原件清单。packet 仅以规范字符串持有内容；既有 packet 实例也重验，不靠 model_construct 跳过其哈希或容量检查 |
| ApplicabilityContext / prepare_context / read_context | context_id 为 app-context- 加完整 payload_sha256。prepare_context 从主机显式焦点构建当前 packet，核验 task/run/源码、诊断、假设、证据、候选及引用闭包，按引用数和读前容量门捕获原件，核定完整输出容量后不可改写发布；read_context 只读取冻结材料、重算原查询并核验原件闭包，不从最新检查重建旧上下文 |
| EvidenceUse / PremiseAssessment | 引用只允许 original_analysis 或 current_candidate；前者不得带候选，后者须有成对候选 ID/执行快照。逐 premise_id 保存 supported/contradicted/unknown、支持/冲突引用及非空 reason。同项引用按 evidence_id 唯一，支持与冲突不得重合；supported 须有支持且无冲突，contradicted 须有冲突，unknown 可无引用或保留尚未解决的证据关系，不能由字段名升格语义 |
| ApplicabilityProposal | 固定 context、经验 entry/content/projection 摘要、当前 task/run/分析源码、diagnosis_id、有序非空 hypothesis_ids 和成对可空候选。逐项且按原指导顺序覆盖全部 premise，不接受新增、遗漏、重复或重排；候选引用还须匹配提案候选/执行快照。运行时核验完整显式焦点、实际返回页成员及当前引用，模型不能通过填写有效 ID 自选其他范围 |
| PremiseQuestion / ReviewPayload / ApplicabilityReview / review / read_review | review 固定外部 binding、context/query/retrieval、完整 proposal 与原 SolverProjection、派生 status/next_action、unknown 原问题及升序唯一当前 obligation_ids；record_id 为 app-review- 加完整规范内容摘要。read_review 从冻结原件重算完整审查对象，不只验证自报哈希。semantic_authority 固定 model_inference，current_evidence_authentication 固定 registered_frozen_sources，focus_recency 固定 explicit_host_input_not_authenticated，application_verification 固定 not_run |

来源权限由主机 registry 赋予。学习准入只接纳 origin_split 为 curated/development 且明确允许 learn 的来源，restricted 一律拒绝；projection 须为 curated_projection/inference_projection 并标 solver_approved，synthetic_history 的原文仅作审计材料。hidden_test、reference_patch、evaluation_label、raw_session 等 role 即使登记在 registry 中也不能成为此包的经验来源；candidate_validation/confirmation 来源不能回流提炼。

冻结或回读快照时再核对**所有原来源 grant 的 solve 用途与目标 split 交集**，不因某一来源只作审计而跳过权限；任一历史 lineage_group_id 落入 excluded_lineage_groups 时拒绝该选择。该范围由主机显式传入，不从标签或 task_id 自动推断谱系。KnowledgeStore.read_projection 只返回已绑定快照中的指导副本，并先核验整张快照闭包；history 原文不随它转成当前 Evidence。空快照可以显式冻结，但缺失或破损快照不会自动退成空库。检索前核对完整请求 scope 与快照 scope，再核验整张来源闭包；权限错误不能变成少返回几项或空结果。

excluded_lineage_groups 只排除已经显式声明的 HistoricalSource 谱系。没有 history 的获准 projection 也可能合法，因此该过滤不能自动证明指导文字或 model_inference 已穷尽来源、未包含目标答案；仍依赖主机完整审阅投影并如实声明来源。未来自动提炼须另增派生依据闭包和学习调用核验，不能沿用此包的字段完整性作为全量来源证明。低层 KnowledgeStore/KnowledgeRetriever 核对给定快照及冻结 scope，检索审计回读重放保存查询，仍不认证调用者实际 task/run。ApplicabilityStore 以外部主机 approval 将具体快照、scope 与实际 workspace/task/run/日志绑定；SDK 普通检索再固定 session profile，并认证实际模型响应、动作和回执，不向模型开放低层自选范围。独立三态接口仍使用显式主机焦点；SDK 外层已有实际发送来源核验，不能由普通检索的授权关联推定最新焦点或语义适用性。

KnowledgeStore 的 root 由受管主机预先建立；constructor 和读取方法不创建目录。发布操作才可在核验该 root 后建立固定 materials/entries/snapshots 子目录，按原字节摘要保存材料、按内容身份保存规范条目和快照；检索显式发布另使用 indexes/retrievals 子目录。已有文件必须字节一致，不能覆盖或修复。先发布材料再发布引用对象，部分失败留下的 pending/孤立文件不取得权威资格，也不自动清理。原材料可保留获准的空白/换行，原字节 grant 摘要与解析后规范对象摘要分开；条目、投影、全部来源权限、历史描述及检索配置均由各自完整内容身份绑定。单产物和 registry 总输入遵循 `knowledge_contracts.MAX_PAYLOAD_BYTES`，快照条目数遵循 `MAX_SNAPSHOT_ENTRIES`，其他长度/集合约束以相应契约模块 schema 为准，不截断或自动放宽。

每次完整引用闭包另受 `knowledge_contracts.MAX_CLOSURE_BYTES` 约束，与单产物限额是独立协议容量。_ReadSet 在某一唯一路径首次读取前按实查大小占用闭包预算，同路径重复引用不重复占容量，但仍重新读取核验；所有公开读取方法使用同一门。admit_entry 在发布任何文件前核对规范 entry 字节与不同摘要原材料的总量；freeze_snapshot 先核对已有读取闭包加最终 snapshot 字节，再发布。检索将来源、索引/描述符及最终记录共同纳入一次闭包容量，即使 query 不写文件也预留完整输出记录；它还按 `retrieval_contracts.MAX_INDEX_READ_BYTES` 和 `MAX_INDEX_READ_CALLS` 限制该次操作的累计原字节读取与调用，finish 的重复读取同样计数。超限拒绝，不通过部分引用“瘦身”，不截断命中项或自动提高限额。记录的 source_closure_bytes 是按唯一路径统计的来源容量，index_bytes 是 SQLite 与描述符原字节总量；二者不是累计 I/O、实际 Token 或耗时。这是有界性保证，仍有重复 I/O，未据此宣称大库性能或实际 Token 收益。

构建和回读按引用重新读取 CAS 原字节，核对文件名/规范内容/摘要、原材料解析全文与已保存对象、权限及引用闭包。路径检查覆盖完整未解析祖先，拒绝链接、重解析点、共享文件和非正规文件；读取前后核对路径及文件描述符，闭包完成后重读全部引用并独立核对 FD change-time 和祖先身份，包括同长度且恢复 mtime 的改写。检索同一次闭包还包括索引、描述符和已保存记录；下一次操作不凭 size/mtime 缓存。此为受管单 writer 的串行主机协议，不宣称 OS 并发锁、原子沙箱或自然语言语义认证。

SQLite 索引仅是快照派生物，不授予权限或证据资格。`indexes/<snapshot_sha256>.sqlite` 与同名 `.json` 描述符由 build_index 显式生成；查询和回读不新建、重建或修复索引。库仅接受固定 meta/entries/tags 结构；search_text 来自获准 guidance 的实际文字值，包括标签、触发、前提/未知问题、步骤/观察分支、保留行为、反例和退出条件，不加入 schema 字段名、premise/step ID、原 history 或审计描述。每次读取原 SQLite 字节，核对描述符与摘要，在内存中 deserialize，用只读连接逐行比对原快照推导的完整行及行摘要，再执行排名查询；额外结构/行、sidecar、缺文件及原字节漂移均拒绝。索引即使重新计算了自报摘要，只要实际行与原快照推导值不符，也不能通过。

tags_text_v1 用规范标签精确匹配；text 按 casefold 后的 `\w+` 提取排序去重词，按各词是否为 guidance 文本子串计数，对应参数化 SQLite `instr()`。得分为冻结的 tag_weight × 命中标签数 + text_weight × 命中独立词数，不按重复出现频次加分。候选集合始终是该快照全部成员，包括零分、未知/缺标签成员；只按分数降序、entry_id 升序分页，不以相关性声称前提成立。limit 为空时使用冻结 max_results，显式 limit 不得越过它；字符限额按 text 加输入标签原文核验，独立词数遵循 `retrieval_contracts.MAX_QUERY_TERMS`。超出 SQL 有符号整数评分能力拒绝，不发生浮点升格或截断。SQLite 原文件/页边界遵循 `retrieval_contracts.MAX_SQL_BYTES`；构建连接和每个读取连接各用 `retrieval_contracts.MAX_SQL_STEPS` 限制执行步数，索引校验与排名是分别受限的连接。协议容量常量由契约代码定义，不复制为本机资源配置，不替代外层受管进程的限制。

独立新对象使用 schema 1、严格禁止额外字段、冻结嵌套 tuple 并重验传入实例。RetrievalRecord 的规范 payload 和哈希绑定完整查询、检索行为、投影、排序/分页及容量；这些内部一致性仍不能单独证明索引行或语义。read_record 使用同一次来源闭包读取保存的原记录，再从原快照、索引和保存查询重算全文，对比记录而不是信任 matched_tags、分数或正分成员数。查询返回的历史指导仍保持原权威与 unknown/failed 等有限历史范围，不生产当前 Evidence、诊断确认或应用接受结果。

ApplicabilityStore 的构造器只保存外部批准参数，不创建文件；bind、record_query、prepare_context 和 review 是显式发布入口，read_query、read_context 和 read_review 是纯历史读取。每个发布入口先预留输出容量及关闭/回读预算，完成已读原件的 finish 核验，再发布并独立回读审计；prepare_context 还须通过下述完整审计序列预检。记录分别进入同 run 的 experience-bindings/approved.json、experience-queries、applicability-contexts 和 applicability-reviews，当前任务原件保存在 applicability-materials/<sha256>.blob。主机批准值与每条记录的绑定摘要均核验，context 和 review 还重核完整当前来源；不能仅凭某个保存对象的哈希自行授予读取其他知识或任务的资格。发布非跨文件事务，失败时可能保留完整但未引用的材料，不能自动填补缺项或把它们当任务事实。

显式焦点必须指定诊断及非空假设；主机核对其实际归属，但不认证它是 SDK 最新已见焦点。当前候选可与焦点诊断直接匹配，或按第 3.1.1 节的失败 previous 关联接纳同归属修订候选；该关联只允许主机选择焦点，不扩大证据范围。当前引用须来自该焦点诊断的已登记证据：source_fact/log_observation 属于原版分析，不可标为 current_candidate；local_execution 及递归依赖它的 model_inference 则必须核对实际 evaluated_candidate_id、execution_snapshot_sha256 与当前候选一致。旧 previous 的失败及其派生推断可以保留在完整上下文，但不能成为新候选的当前支撑。引用的存在、来源和版本由主机核验，引用对前提的语义支持程度仍是模型判断。

review 保留每项前提及理由，整体状态按 contradicted 优先、unknown 次之、其余 supported 派生。supported 只提供 offer_process，即完整可尝试指导与当前焦点假设/候选的已登记验证义务；contradicted 为 avoid_copy，即暂停照搬并保留冲突；unknown 为 clarify，即保留每项 unknown 对应的原 unknown_question，不自行改写成更强的问题。存在 contradicted 时仍保留其他 unknown 的问题，未知关系不能因整体分支而消失。三态均不授予工具或探针权限、不硬拒绝候选、不改变 Diagnosis/RepairObligation 状态或共同接受门。能力或预算不足时，后续 SDK 可继续通用路径或报告 unresolved；该回退尚未由本独立 API 调度。可信适配器若核验明确谓词，须另记谓词及范围，不能自动确认整项业务语义。

历史 context/review 的分析内容只使用当时不可改写 CAS 原件，并重核原 task/workspace、源码/日志、诊断、义务、证据、候选和检查闭包；入口仍核对外部 approval 与实际工作区、任务和日志身份，原指导同时从固定知识快照及保存查询重算。冻结诊断的原 bundle 从 CAS 回读后还重跑 validate_references，使用冻结源码的实际行数核对引用、位置和源码版本；给伪造 bundle 及外层记录重算哈希不能绕过原准入约束。正常后续检查不重建或覆写旧上下文，继续分析须 prepare_context 生成新源点，再提交新的提案；旧 context/review 保留当时已知与未知状态。CAS 损坏、缺失或内容漂移拒绝，不能回退到 live checks、最新版库或模型自述来修补历史。

容量由 applicability_contracts 的 MAX_CONTEXT_BYTES/MAX_CONTEXT_FILES 及各原件上限约束：知识原件与派生检索闭包、当前任务原件、本地绑定/上下文/审查记录和待发布输出按实查字节保守合计；不同 root 不做全局 CAS 去重，逻辑路径相同内容也不保证只计一次。捕获前先检查引用数，读取前核对剩余容量，发布完整输出前核定总容量，超限拒绝，不截断前提或原件。新 local capture/audit 阶段以 MAX_CONTEXT_READ_BYTES/MAX_CONTEXT_READ_CALLS 累计读取，包含关闭时原字节重读。prepare_context 发布前纯模拟实际下一次 read_context 的完整审计序列，核定审计容量及累计字节/次数：owner 原件与其 CAS 副本是不同路径，分别计费；多项逻辑材料引用同一 CAS 时初读逐引用计费，闭包容量及关闭重读按唯一路径计算。预检及当前原件闭合全部通过才发布，关闭后不再扩展读取或新增发布预留，重复关闭不重读收费。KnowledgeRetriever 的来源/index/记录读取另按自身阶段限额核验，不把这些独立阶段说成一次全局累计 I/O 门。

既有 build_working_context 前段的任务/来源扫描仍没有累计 I/O 门，受外层受管 Job、单来源和最终 packet 限额约束；不能声称 prepare_context 整个操作的全部扫描都已由新增累计读计数覆盖。_capture_material 新增可选 reader/max_references 供本入口捕获时计数，旧 SDK 默认调用方式保持原行为。普通检索工具的实际固定/模拟 solver 调用和动作关联已进入原账本；独立三态及历史提炼声明仍不因此取得真实调用成本。字节容量与读取次数不等于 Token、耗时、语义准确性或修复收益。SDK 三态提案引用实际 solver 和动作账本，共用原有成本，不因独立模块的声明而取得真实云端或应用效果证据。

普通检索与三态 SDK 入口分别保存其运行身份、实际工具回执、报告、聚合及私有导出；受管验收结论见 STATUS。发布别名、静态 Skill 的 Agent 会话交接、自动提炼/学习调用仍待实现；静态资源独立入口见下节，普通检索不能充当适用性验证或演进收益。

三态 SDK 入口须显式提供 `ApplicabilityPolicy`，冻结完整 `applicability_profile={schema_version:1,enabled:bool}`；未提供时保持普通经验检索代。开启与关闭两组提供同名同定义的 `prepare_experience_context`、`review_experience`、`read_experience_review` 三项工具，以及原有完整指导和通用工具，均受原模型/动作预算约束。关闭组只返回完整普通指导，不发布三态上下文或审查，也不解析评语来施加三态引用约束；普通权限和参数形状检查继续执行。开启组准备响应直接返回 review_proposal_schema，模型按此提交格式及冻结 context 引用判断；缺少已见诊断或假设时返回 unknown 与普通检索，不从最新登记记录猜测焦点。

`SolverVisibilityStore` 在 solver reserve 后、实际发送前冻结 `solver-inputs/model-N.json`：保存最终 LangChain 消息、规范固定请求或 PreparedRequest、模型意图、工作上下文及压缩计划。请求体、消息、工具、参数与账本摘要逐项重算。read 仅允许 completed 的实际 solver，audit 保留 reserved 缺输入等未执行状态；缺账本时 audit_stored 只核原件内部一致性，不认证实际使用或可见权限。文件、累计 namespace 与发布容量受模块常量约束，超限停止，不提高模型消息上限。

工具通过主机 middleware 的 ContextVar 取得当前父调用，模型不能提交父调用或焦点。可见依据仅来自该调用实际发送的完整 AI/ToolMessage 组，以及与已核验计划完全相同、实际保留的工作上下文面板。摘要中的文字和 ID 不授予引用权限；同一响应内兄弟工具后来生成的查询、诊断、上下文或检查不能充当该父调用已见依据。简单诊断若只返回 ID，须实际读到完整 inspect_analysis 结果才取得假设；完整面板可提供同样依据。prepare 对当前闭包逐项核对上述可见性；review 必须已见完整 context、使用该 context 的证据并与父调用焦点一致；读取旧 review 也须同焦点。旧候选事实仍不能支撑新候选。

SDK 外层报告以 `focus_authority=actual_solver_input` 保存这项额外认证；独立 ApplicabilityStore 的原始对象仍保留其显式主机输入边界，不覆写旧对象的 focus_recency。两层均保持 semantic_authority=model_inference，支持判断不变成确认根因或应用通过。完整 context/review 与实际请求、动作、回执关联，未关联产物保留为孤立原件；新增调用没有独立免费预算。

发布前按双份 ASCII 报告表示预检三态完整记录、材料列表及共同调用/动作额度的来源链接余量。报告来源链接保存完整焦点和有序可见动作集合的摘要及数量，实际请求、完整指导和动作原件继续保存并从原件重验；不在每次回读关联里重复整段历史。预算耗尽后的拒绝行可能超过 max_tool_calls：逐行绑定实际父调用及请求，完成行还核验拒绝回执，再按父调用汇总数量、已保存/未知数量和有序关联哈希，不把它们标成经验使用。完整动作账本仍决定调用和拒绝成本，未知执行不删掉。报告生成另检查两处完整三态片段的实际 ASCII 缩进大小，超限拒绝。此门和普通查询门分别覆盖新增领域片段，不保证全领域报告的整体容量；矩阵另有公共输出门，按实际 canonical UTF-8 表示在每个 row、variant 和配对追加前计量来源字段、完整摘要及换行，最后再核完整结果。该门保留合法旧代内容及哈希，不覆盖输入树快照、总 I/O、运行内存或单项构造峰值；全链路容量仍须后续收口。

SDK 搜索发布前还须通过 knowledge_handoff.MAX_KNOWLEDGE_REPORT_BYTES：按实际写入器的 ASCII 转义表示、保守缩进和报告顶层/completion 两份，合算未来全部完整查询页、绑定及知识配置，预留所有孤立记录 ID、共享 max_tool_calls 对应的动作关联和固定事实键。UTF-8 原件容量通过不代表该报告预算通过；即便本轮之后因 context_limit 停止，也必须保留可回读的失败证据。超门返回明确拒绝，不发布该查询、不截断指导、不放宽模型上下文或聚合单文件上限。此门限制新增知识片段，不声称已经覆盖其他领域产物或全矩阵汇总的所有容量。

Agent 启动和恢复绑定具体已物化快照，SQLite 原件不得依赖未合并 WAL。知识恢复要求主机显式提供原 knowledge_approval，并从 session 冻结 profile 和 run 原件核验；不要求外部库存在，不重新 prepare，不换最新版或空库。未来发布别名只影响新任务，不改变旧 run。新知识配置进入相应新会话、报告、导出和共同评测身份，不补写旧运行；普通检索和增强组读相同投影，基础组继续保留通用读取、反思和验证能力。Skill 版本及演进的后续义务见第 3.1.3 节。

受管固定/模拟脚本 schema 5/6 必填 working_context 和 knowledge，旧 1–4 不接纳知识字段。knowledge 含仓库内规范 library_path、完整外部 TaskKnowledgeBinding，可选完整 ApplicabilityPolicy 和 StaticSkillPolicy；静态选择必须同时显式提供三态策略。启动只读核批准与任务身份、原件、冻结索引和权限，不扫描目录生成知识或建立空库。恢复从脚本取得原外部批准，以 run 原件核验知识和策略；library_path 是启动定位符，允许迁移或不存在，仍核词法范围，不能作为当前批准来源。批准/策略/选择/模型/预算漂移及准入阶段坏账本统一为只读拒绝，CLI 返回 3，worker 不覆盖旧 run-state。开始执行之后的失败仍保留正常失败语义。知识、脚本、会话和清单的分代独立，不将脚本 5/6 当成自动学习资格。

反馈来源另由 `feedback_contracts.py` / `feedback.py` 定义，不改变旧 ExperienceDraft/ExperiencePayload 的 schema 1。`FeedbackStore` 面向已停止的开发运行，先核验事实来源，再接纳待审核经验；其程序接口不等于已接云端自动学习。

| 反馈对象/入口 | 约束与行为 |
|---|---|
| HostFeedbackApproval | 外部主机批准完整单运行评测清单的内容身份、development 学习用途、允许部署分区和排除谱系。清单须含准确 CaseSelection，核 task schema 2 的选择哈希；该绑定不证明真实谱系独立性。确认/候选验证来源不能靠重标 development 回流 |
| capture / FeedbackCapsule | 只读核终态、原任务、报告、完整候选与检查、模型/动作账本和全部知识祖先的学习权限；保存精确原字节与空目录清单，原端和副本重聚合结果须相同。Graph 不参与反馈核验且不复制或执行。失败及结果未知继续保留，终态不是修复成功 |
| read_capsule | 需要重新提供原外部批准，从 capsule 原件核精确清单并重聚合；不要求原任务、run 或外部库存在，不创建目录、修复数据库或补齐记录。缺失/改写/链接/未知条目及未合并 SQLite 写入拒绝 |
| HostCandidateApproval / propose_candidate | 外部主机将完整投影身份绑定原 capsule，人工明确提出适用前提、查证过程、保留行为、反例和退出条件。首版投影仅 host_curated，validation_scopes 为空；原合成范围保留在 capsule，不据此证明整套新过程通过验证 |
| ExperienceCandidate / read_candidate | 不可改写的 pending_host_review，application_qualification=none；回读重新核外部批准、原 capsule 和完整投影。没有原运行或成本依据不补零，不自动入旧经验库、生成发布别名或激活 Skill |
| 成本与容量 | 原修复成本取独立重建的完整账本，失败/未知仍在分母；首版候选为主机提议，新增学习模型调用 not_run，模型成本保持 null，不冒充自动提炼。单次 API 的复制/关闭重读共用 FeedbackLimits 累计读取预算，元数据、原件与各回读阶段不能重新领取额度；完整文件和总容量有门。该计量不包含聚合内部 I/O，不声称全流程耗时/内存已闭合 |

反馈摘要中的定位、归因和义务解释仍是推断，程序认证的是原记录、版本和权限关系；应用验证继续 not_run。后续模型提炼须先显式接新调用用途及其恢复/成本账本，主机批准桥接须保留完整 capsule 来源，不能给旧 schema 补字段冒充新资格。该机制给 Skill 演进提供可复核输入，候选内容的迁移质量及论文收益仍须独立实测。

#### 3.1.3 Skill 版本、验证与演进

**静态过程先提供完整可读的诊断步骤，后续演进再决定是否应更新这些步骤。** `static_skills.py` 从同 run 批准知识中选择人工整理的条目，生成受限的 `SKILL.md` 并复用锁定 Deep Agents 的 `SkillsMiddleware` 加载/注入。`static_skill_handoff.py` 将完整读取、实际模型输入、动作账本与报告接起来；程序入口、启停对照和私有导出使用同一冻结身份。受管脚本/CLI 配置已接入，反馈演进仍待实现；合成调用及可见性不证明学会经验或提高修复率。

| 静态入口/对象 | 当前约束 |
|---|---|
| StaticSkillPolicy | 完整 schema 1、显式 enabled 和升序唯一 entry_ids；只接受批准快照成员且 derivation_authority=host_curated。未指定不是开启空库，未知成员、模型推断投影及他 run 批准均拒绝 |
| prepare_run_static_skills | 使用同 run 已物化的知识和外部 TaskKnowledgeBinding，主机应通过 knowledge_run 准入后调用。编译核对 owner、绑定批准及快照完整来源/权限/范围，不消费或认证检索索引。Agent 传入完整共同 max_calls/max_tool_calls，发布前核验资源、关闭读取、原生元数据及双份 ASCII 报告表示余量，随后发布 static-skills-profile.json 及 static-skills/<内容身份>/SKILL.md。失败可能留下局部资源；不自动续写、覆盖或将残留当完整发布 |
| 冻结 profile 与正文 | profile 完整保存开关、选择、快照、renderer、提示摘要、文件摘要/大小及原 SDK 元数据。正文保留前提和未知问题、步骤及观察分支、保留行为、反例、退出条件和完整 canonical SolverProjection。描述不得被 SDK 截短；内容身份绑定投影摘要。原 KnowledgeSnapshot 的 skill_files 仍为空，静态资源另有 profile，不改旧代含义 |
| 同资源启停 | 两组生成完全相同正文和元数据，shared_static_skill_profile 仅去掉 enabled。开启注入含 read_skill 调用说明的目录；关闭仍加载、核验相同元数据并允许读取完整正文，只取消目录注入。工具定义和完整读取结果均相同，普通检索结果中的投影摘要可按共同工具说明推导同一 Skill ID；没有额外调用、工具或 Token 预算 |
| StaticSkillService / read_run_static_skills | 构造不创建资源；回读以调用者冻结 profile、外部批准及 run 内来源重编译核对全部原字节。外部库可不存在，不能依赖本地 profile 自报权限、最新快照或检查点缓存；未知 ID 不拼接路径，缺失/改写/额外成员、链接与共享文件拒绝 |
| FrozenSkillsMiddleware | 用 SDK 原生发现、YAML 元数据解析和 system message 注入，薄层约束负责完整批准清单、资源回读和缓存一致性。before_agent 与 modify_request 均核原件；已有缓存须与原元数据完全一致且加载错误为空，不能静默跳过失败资源。恢复在可写账本前只读检查锁定 SQLite saver 最新检查点及未合并的 Skill channel writes；准入与实际静态运行均显式使用 SDK 严格 serializer。最终 solver 输入在派发前验证唯一、完整、末尾原生目录块，关闭组不得含该目录；异步加载采用相同验证入口 |
| Backend / 容量 | 后端仅枚举批准的虚拟根和下载精确清单内 SKILL.md；拒绝目录扩展、源码读取、支持文件、写入及执行。单文件、条目数和资源/profile 总量受 static_skills 常量约束；静态报告双份片段受 static_skill_handoff.MAX_STATIC_SKILL_REPORT_BYTES 约束，启停都预留完整目录余量，最终再次按实际 ASCII 缩进检查。实际 solver 原件审计沿用其共同单件/总量门并计入关闭重读；不将知识的独立闭包门误当 solver 总量门。这些分阶段门不是整轮全局 I/O 或全领域报告容量保证 |

目录提示说明人工静态指导没有当前事实、应用资格或新的文件/执行权限；完整正文只能提供待核对的步骤，不能直接满足修复义务或接受门。显式 StaticSkillPolicy 要求批准知识及显式 ApplicabilityPolicy，开启和关闭均使用 session/report/completion 13/10/7 与相同 20 项工具；未指定静态策略保持原代，拒绝静态字段、工具和资源残留。read_skill 只读选中的完整正文、元数据及内容身份，经既有 read_source 能力、父模型响应和动作预算授权，结果未知不重派。报告分别记录成功完整读取及其后真正可见的 solver 数量/有序身份摘要；没有后续模型输入时不宣称已使用。未知/拒绝/孤立记录和全部实际成本保留，原件、完整回执及实际输入随私有包导出；缺账本只能核原件，使用和成本保持未知。恢复须提供原外部批准与完全相同策略，不要求外部库存在。清单 5 固定共同知识与 shared_static_skill_profile，变体显式登记 static_skills_enabled；底座模型、工具、预算和接受门保持相同。实际能力与完成证据以 STATUS 为准。

SkillVersion 保存 skill_id、内容/父版本、来源经验、适用前提、必需输入、查证及观察分支、修改建议、保留义务、退出/反例和实际验证范围。SDK 的 SKILL.md 与支持文件是受控投影，全部实际可读文件进入快照；首版投影限说明性内容，不附自动执行脚本、不越库读文件，加载也不替代既有工具授权。四类 RuleCard 可成为人工种子静态 Skill，不能算学习后晋升成果。

演进使用追加 SkillTransition，内容版本与发布别名分开。接口为 propose_skill_revision、record_skill_validation、decide_skill_transition；transition 绑定父版本、候选内容、允许反馈、归因提议、冻结验证计划、实际回执、成本及发布决定。

| 状态/动作 | 接受条件及下一步 |
|---|---|
| proposed → validating | 修改范围、来源与允许分区可核验，验证协议/对照/成本已冻结；否则 pending |
| validating → pending / rejected / eligible | 证据不足、未知成本或门槛未定为 pending；按冻结规则明确不合格为 rejected；只有完整实际证据满足策略才 eligible，模型自评无资格 |
| pending → validating | 缺项补齐后追加新的验证尝试和冻结计划身份，保留原 pending、未知成本及旧计划；不原地改写。已 dispatched 而结果未知的检查不盲重发；新尝试另占预算，所有成本继续保留 |
| eligible → activated | 发布事件指向不可改写版本及验证依据；新任务解析发布别名，在跑任务仍读原快照 |
| 缩界/补分支 | 形成新候选及父版本关系，重新验证，不原地覆盖旧 Skill |
| 退回 | 追加激活事件指向仍可读的旧版本；不删除失败成本或改写在跑任务 |

失败首先保存候选/版本/实际观察，并区分选错、步骤缺陷、未遵循、环境/工具失败或未知的归因提议。主机能核验失败及版本，归因仍可能是推断；环境失败或结果未知不能直接推成“修法有害”。被 rejected 的内容修改形成新候选，不覆盖原拒绝记录。合成验收只授予 synthetic 状态转换资格，不生产真实应用 eligible/晋升证据。

晋升策略由评测文档定义，执行配置冻结其摘要：独立开发验证谱系、适用边界反例、旧/新同协议比较、功能/回归、质量损失、成本及未知处理。数值待定不阻塞合成软件实现，但禁止真实晋升。学习、候选验证及确认材料权限分开；主评测冻结，确认反馈不回流选候选；持续学习仅按允许的先测后更新协议另评估。

检索、适用性提案、经验提炼、Skill 修订、候选验证与失败回退均登记归属和成本。来自已有 solver 的提案引用原调用，不重复记费；新增模型调用必须先 reserve，再使用明确的学习用途。现有 ModelCallIntent 的四类用途不得以 solver 标签掩盖新增学习成本；兼容字段和工作流须在接入前迁移。学习与部署分开汇总并给总成本。

#### 3.1.4 必须验证的反例

| 输入或故障 | 必须表现 |
|---|---|
| 相同异常，字面搜索与有意正则的业务义务相反 | 不自动复用旧修法；冲突保留，未知提供查证问题 |
| 很早的义务/失败仍与当前决定相关 | 必需闭包保留；后来“已修好”的模型文字不消除反例 |
| 历史候选 A 失败，当前 B | A 的观察保持原版本，只作相关历史指导；不成为 B 的检查结果 |
| 必需引用缺失、来源被改或包超过额度 | 拒绝或明确限额停止，不悄悄删记录 |
| 多工具组只留下部分结果 | 实际发送选择拒绝，原 Graph 不变 |
| unknown/缺平台标签、没有诊断 | 保留未知及通用路径；不先过滤再伪称判别成功 |
| 原投影被改而索引/别名未变，或旧快照丢失 | 回读拒绝；不能换当前库继续 |
| 更新别名后旧任务恢复 | 继续原快照，新任务才可使用新版本 |
| 合成通过、一次成功、成本未知或门槛未定 | 不取得真实晋升资格 |
| 未遵循/环境未知被提炼成全局修法缺陷 | 归因不升格，失败及未知成本保留 |
| 确认反馈回流或关闭判别后删对照前提 | 来源权限/公平比较拒绝，不能隐藏损害 |

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

本节规定目标字段和行为；目录与资源机制见开发设计第 9 节，数值只读取 configs/local-runtime.json。LocalOwner、CleanupPlan 与 CleanupReceipt 已由 W01 的 PowerShell 管理入口部分实现；W02/W03 已建立 Job 限制、活动标记与生效限额记录，并沿用到离线 Agent、聚合及 Export。私有导出对象见第 1.3.4 节，实际验收见 STATUS。下表完整的 ProcessLease/EffectiveResourcePolicy/LocalUsage schema、正式 Agent 和本机应用作业接入仍待实现，现有 runtime.py 不包含这些能力。

| 对象 | 必须字段 / 约束 |
|---|---|
| LocalOwner | schema_version、project_instance_id、repo_real_path、managed_real_path、created_at；路径与当前仓库匹配，不把 owner 文件当任意删除权限 |
| EffectiveResourcePolicy | policy_sha256、解析后的绝对路径、生效的 CPU/提交内存/进程数/墙钟限制、磁盘预算与预留线、enforcement_capabilities；无效范围/未知字段拒绝，不接受 NaN/负数或大于 1 的比例 |
| ProcessLease | project_instance_id、run_id、Job 标识、supervisor PID/创建时间/路径、进程成员、启动时间、截止时间、状态；PID 必须和创建时间一起核验，Job 句柄只存在于运行进程 |
| LocalUsage | 测量时刻、各类字节、磁盘空闲、可用物理内存、受管 committed memory、峰值与执行能力；managed 与 exports 分别报告、合计受共同预算约束，逻辑大小不能冒充精确可释放物理空间，未知不填 0 |
| CleanupPlan | plan_id、实例 ID、scope、规范目标列表、估计字节、保留项、证据导出状态、本机未结构建/设备动作及历史远程标记、目标身份/状态摘要；Apply 前重新核验，不能直接信任可编辑 JSON 中的路径 |
| CleanupReceipt | 实际删除/保留/失败项、时间、关联 plan_id、剩余占用；部分失败不记完整成功，重复执行可安全恢复 |

受管根只接受设计指定的仓库内目录。拒绝空路径、卷根、仓库根、上级/同前缀目录、路径链中的链接与目录外链接目标；管理根内部的 uv/Python 链接须解析核验并只删除链接本身。解析失败则不清理。清理流程持有独占维护锁并阻止新工作；进程或设备动作未结束、文件仍在用、证据未处理或旧远程作业状态未知时返回阻塞原因。测试应覆盖扫描之后发生路径替换的情况；本机同用户恶意篡改不能仅靠应用层路径检查获得完全隔离保证。

资源中断不得归因成补丁功能错误：result 使用 inconclusive 或 blocked，并记录 failure_kind=resource_limit / timeout / interrupted 及原因；已产生候选和所有成本保留。任务停止归入 budget_exhausted（资源/时间上限）、blocked（保护器不兼容或环境不可用）或 unresolved（用户停止），不新增另一套成功语义。操作系统内存上限触发可能导致分配失败而非自动退出，管理器仍须检测并结束/报告。

单个导出包的预计完整字节受 limits.max_export_package_bytes 约束，数值只在配置维护；所有受管任务的预检/监测把 managed 与 exports 合计纳入既有 managed_disk_budget_bytes，并核对卷空闲预留。Export 复制前预估完整包、清单和报告；逐次写入检查卷剩余空间及已写/待写账面量，完工再扫实际占用。已复制字节不能再次作为新增预留，分配失败或超限不发布包；磁盘监测不承诺内核硬配额。

Stop 不隐式删除文件；Clean 不隐式杀未知进程。当前非空 work 即使全部终态，清理预览及 Apply 仍须明确 -DiscardEvidence；cache/tmp/all 检测到 task.json、source-manifest.json、package.json 或 ex-* 未发布导出目录时同样要求明确丢弃。all 中 artifacts/state 证据仍阻止无丢弃授权的清理。Export 仅覆盖 manifest 声明范围，不覆盖所有其他运行和管理记录，不自动解锁整树清理；用户应先核验需保留的包，再在具体清理请求中决定是否丢弃原记录，不能先清证据再补报告。已导出包由普通清理保留。受管 Job 外的既有设备服务须单独核验归属和停止能力；云端模型请求已发送但本机失联时仍按模型账本保留未知费用与结果，不假定 Stop 能撤销请求。

## 6. 关联与待完成规格

工作包顺序、具体未闭合项和完成条件统一见 [实施计划](project-work-plan.md)；实现进度见 [STATUS](STATUS.md)，对照与贡献归因见 [评测口径](preparation/metrics-and-gates.md)。因果记录、有限条件结果到修复决策和候选的离线交接、mock wire 模型边界、主机结果摘要、执行来源冻结、只读离线聚合和本机私有 Export 已有代码入口，实际验收以 STATUS 为准；正式应用汇总、可重建主机材料与应用工具链来源、公开脱敏/提交、真实提供方、真实值采集、其余规则检查、可信应用验证接纳与本机执行控制仍待闭合和实测。
