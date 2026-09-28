# 模块契约与规则规格

状态：当前规格草案，离线闭环、模拟远程协议和策略分支部分已实现。最后维护：2026-09-28。
职责：维护对象不变量、控制器分支与规则卡。配套 [总体技术设计](development/system-design-draft.md)。证据/诊断/义务/候选基础对象、固定响应离线循环、模拟远程协议及 W07 策略分支已有合成实现；完整因果/动作/应用验证对象仍是待实现规格。文中案例推演属于分析人员材料，不输入未来的独立评测求解器。

## 1. 数据契约与不可破坏的条件

离线原型的 schema_version=1 覆盖输入和栈证据；W04 对首批领域对象也采用 schema_version=1，但各对象分别校验，不能把现有 evidence_ready 报告直接当作 Diagnosis。下表其余字段仍是后续目标契约。

### 1.1 版本与来源

所有记录包含 schema、task_id、run_id、record_id 和 created_at。所有涉及源码的记录绑定 source_snapshot_sha256；分析源码和完整运行源码分别使用 analysis_snapshot_sha256、runtime_snapshot_sha256，不要求二者相等。二者之间通过文件清单、共享业务源码哈希及打包记录建立关联。

SourceLocation 包含 snapshot_sha256、相对 path、1 起始的 start_line/end_line 和可空 symbol；修改前与修改后的坐标必须分开。禁止使用单一的 confidence 浮点数替代证据。

Evidence 至少包含：

- kind：log_observation / source_fact / model_inference / local_execution / application_execution。
- artifact_sha256，以及适用的日志行、源码位置或 execution_id。
- statement、直接引用的依据 evidence_ids、derivation。
- source_snapshot_sha256；无关源码的纯日志项可空，但日志必须绑定任务输入。

引用检查器只负责存在性和版本一致性；模型推断不会因为引用合法就变成运行观察。运行产物须由执行适配器登记，不能接受模型自行填入 application_execution。

### 1.2 诊断对象

Diagnosis 保存 exception_locations、hypotheses、selected_hypothesis_id、unknowns。每个 Hypothesis 包含：

| 字段 | 内容及约束 |
|---|---|
| root_cause_locations | 原始源码位置列表；允许暂空，不用异常位置填满未知根因 |
| constraint | family、适用操作、变量绑定、需成立的条件、该条件的语义来源 |
| causal_steps | 从值或状态产生到异常操作的有限关系链；每一步有依据或明确推断标记 |
| support_status | supported / contradicted / unknown；这是证据判断，不是正确率概率 |
| evidence_ids | 支持和反驳分开存储，不能仅保留支持证据 |
| missing_evidence | 哪个事实未知，以及为何可能改变修改方向 |
| distinguishing_check | 至多一个优先检查建议，包含不同结果会导致的决策；可空 |

首批边界对象已用 Pydantic 2 实现严格字段、来源、源码版本、行号和 evidence_id 校验；证据由可信调用方通过 `admit_evidence` 标注来源，模型响应不能自称执行后端。W05 的固定响应循环已把假设作为 `unknown` 写入记录，并用 SQLite 保存 SDK 对话检查点；它尚未实现完整因果链或可信根因确认。因果链本身的正确性依赖后续检查，不能通过 JSON 校验宣称成立。

### 1.2.1 修复义务和动作提案

RepairObligation 包含 obligation_id、kind（运行条件/业务行为）、statement、bindings、source_evidence_ids、authority（已核验需求/接口规则/既有行为/模型推断）、checkability、status、scope 与依赖版本。Hypothesis 关联 obligation_ids；CandidatePatch 关联 restores 与 preserves；Check/Result 关联所检验或违反的义务。

模型推断的义务只能作为假设，不得仅因某候选违反该推断就机械拒绝它。局部执行只能支持对应输入与路径，不自动证明全局成立。可信语义不足时明确 unknown。

ActionProposal 包含 action、target_ids、reason、required_capabilities、expected_observations、decision_if_observed、estimated_cost_basis。针对探针，target_ids 指向待区分的根因/修法；未知结果分支必须存在。控制器验证引用、权限和预算，不能把模型预期当执行结果。

反例 Counterexample 保存实际输入或产物引用、执行版本、观察、violated_obligation_ids 及 affected_hypothesis_ids。关联本身也可能是模型推断，必须标记；只反驳观测所覆盖的主张。重试时优先携带有效反例与未知项，不重复携带所有长日志。

### 1.3 补丁、验证和尝试

CandidatePatch 包含 candidate_id、ordinal、parent_snapshot_sha256、diff_sha256、diagnosis_id、intended_constraint、restores、preserves、preserved_behaviors、changed_paths、verification_obligations。完整 diff 登记后才进入任何候选专属检查；格式错误、无法应用和局部检查失败同样记为已产生候选的失败。

没有生成完整候选的模型失败记录为 generation_failure，计入总成本，但不伪造一个可验证候选。首次候选通过率同时报告“全部任务”为分母的端到端口径，避免把未生成补丁的任务排除后抬高成绩。

W04 的候选工作区从已校验分析快照建立独立草稿；精确文本编辑在临时副本完成后切换，失败不改原草稿。登记时冻结完整 diff、候选源码副本和哈希；仅登记且复核未变的候选可交给后续检查。修订时可以读取前次 diff 和反馈，但新 diff 表达相对该基准的完整修改。该工作区尚不是可构建的运行工程；跨候选诊断中的旧执行证据保留历史版本，只能说明对应旧版本。

W05 的合成执行器只对冻结候选做固定文本检查；先写入含候选/检查/协议 ID 的意图，重启后用同一幂等键重试或读取已有结果。真实远程工作者尚未接入，模拟 HTTP 服务的幂等性不能代替设备端核验。`offline_checked` 不是 VerificationResult 中的应用 pass，也不能满足最终共同协议；`application_verification` 始终为 `not_run`。模型工具保留哈希校验日志读取、通用源码浏览、精确编辑和候选登记，关闭 SDK 的任意写文件、shell 和委派工具，以免绕过登记门。

VerificationPlan 中每个 Check 包含 check_id、level、required、hypothesis_or_behavior、oracle_origin、input_class、expected_relation、capability、timeout 和 protocol_sha256。

- oracle_origin 区分题目/已核验需求、既有正确行为、独立验收测试、模型临时假设。
- 模型临时自测可帮助筛选；它本身不能成为“符合业务预期”的唯一证明。
- 验收协议在观察候选成绩前固定；候选专属诊断探针不能替代共同验收协议。
- 故障构造测试、要求错误代码形态的断言，先归为构造材料，不能原样充当修复验收。

VerificationResult 必须关联 check_id、candidate_id、execution_id、实际源码/产物哈希、protocol_sha256、environment_id、status、failure_kind、artifact_refs、duration_ms。只有相同版本和协议的结果才可汇总。result 缺失、执行中断或材料不全归为 inconclusive 或 blocked，不补写 pass。

W06 的 `ExecutionRequest` 先绑定 task、candidate_id、完整运行源码哈希、补丁哈希、环境、共同协议、初始状态、执行层、重复序号和超时。`run_id + 请求规范摘要` 生成幂等键；重复序号改变表示一次新的逻辑回放。客户端提交前保存意图，响应丢失时只能以同一键对账；服务端 SQLite 对该键设唯一约束，查询不能新建作业。`cancel_requested` 不是 `canceled`，服务重启后无法确认仍在运行的模拟作业标记 `inconclusive`。产物引用绑定 job、内容哈希与大小，服务和客户端分别校验。模拟结果显式 `scope=synthetic`、`application_verification=not_run`，不能转为正式应用 pass。当前协议仅由本地模拟服务测试，真实执行进程、设备状态确认与应用级 VerificationResult 仍待接入。

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

W07 已把下面四张规则卡的压缩执行提示登记在 `policies.py`，通过 `consult_rule` 访问；未知类别或关闭规则开关时返回通用路径。`plan_repair_action` 接受当前源码版本、修复方向及观测到方向的映射；若修法和验收相同，直接建议生成候选。不同修法只有在观测可改变修法且能力/预算允许时才建议查证。当前 Agent 主机只授权已有日志/源码读取，没有诊断执行探针，因此模型自行填入探针能力或预算不会令其运行。建议本身不执行检查，也不判定根因正确。

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

下表来自此前静态阅读，作为设计推演而非独立效果评测。准确行号与来源以 [数据核查报告](preparation/data-b-assessment-2026-09-15.md) 及固定源码版本为准。

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

本节规定目标字段和行为；目录与资源机制见开发设计第 9 节，数值只读取 configs/local-runtime.json。LocalOwner、CleanupPlan 与 CleanupReceipt 已由 W01 的 PowerShell 管理入口部分实现；W02/W03 已为合成、Init、测试与质量 Run 建立 Job 限制、活动标记与生效限额记录。下表完整的 ProcessLease/EffectiveResourcePolicy/LocalUsage schema、正式 Agent 任务接入及远程状态联动仍待实现，现有 runtime.py 不包含这些能力。

| 对象 | 必须字段 / 约束 |
|---|---|
| LocalOwner | schema_version、project_instance_id、repo_real_path、managed_real_path、created_at；路径与当前仓库匹配，不把 owner 文件当任意删除权限 |
| EffectiveResourcePolicy | policy_sha256、解析后的绝对路径、生效的 CPU/提交内存/进程数/墙钟限制、磁盘预算与预留线、enforcement_capabilities；无效范围/未知字段拒绝，不接受 NaN/负数或大于 1 的比例 |
| ProcessLease | project_instance_id、run_id、Job 标识、supervisor PID/创建时间/路径、进程成员、启动时间、截止时间、状态；PID 必须和创建时间一起核验，Job 句柄只存在于运行进程 |
| LocalUsage | 测量时刻、各类字节、磁盘空闲、可用物理内存、受管 committed memory、峰值与执行能力；逻辑大小不能冒充精确可释放物理空间，未知不填 0 |
| CleanupPlan | plan_id、实例 ID、scope、规范目标列表、估计字节、保留项、证据导出状态、远程未结作业、目标身份/状态摘要；Apply 前重新核验，不能直接信任可编辑 JSON 中的路径 |
| CleanupReceipt | 实际删除/保留/失败项、时间、关联 plan_id、剩余占用；部分失败不记完整成功，重复执行可安全恢复 |

受管根只接受设计指定的仓库内目录。拒绝空路径、卷根、仓库根、上级/同前缀目录、路径链中的链接与目录外链接目标；管理根内部的 uv/Python 链接须解析核验并只删除链接本身。解析失败则不清理。清理流程持有独占维护锁并阻止新工作；进程未退出、文件仍在用、证据未处理或远程作业未知时返回阻塞原因。测试应覆盖扫描之后发生路径替换的情况；本机同用户恶意篡改不能仅靠应用层路径检查获得完全隔离保证。

资源中断不得归因成补丁功能错误：result 使用 inconclusive 或 blocked，并记录 failure_kind=resource_limit / timeout / interrupted 及原因；已产生候选和所有成本保留。任务停止归入 budget_exhausted（资源/时间上限）、blocked（保护器不兼容或环境不可用）或 unresolved（用户停止），不新增另一套成功语义。操作系统内存上限触发可能导致分配失败而非自动退出，管理器仍须检测并结束/报告。

Stop 不隐式删除文件；Clean 不隐式杀未知进程；删除 artifacts/state 前要有可核对导出或用户明确丢弃，不能先清证据再补报告。受管目录外的模型/设备作业需独立取消核验；本地失联保持未知。

## 6. 关联与待完成规格

工作包顺序、具体未闭合项和完成条件统一见 [实施计划](project-work-plan.md)；实现进度见 [STATUS](STATUS.md)，对照与贡献归因见 [评测口径](preparation/metrics-and-gates.md)。首批字段与固定响应离线控制边界已可执行；完整诊断因果链、动作/应用验证对象和远程控制仍是待完成规格。
