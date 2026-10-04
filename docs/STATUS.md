# 当前状态

状态：当前进度。最后维护：2026-10-04。
职责：唯一维护已完成工作、当前任务、阻塞及下一步；项目蓝图与目录见 [开发导览](../DEVELOPMENT.md)，阶段定义见 [实施计划](project-work-plan.md)。

## 当前阶段

P0 设计闭合进行中，P1 已打通任务策略、跨文件检索、固定/模拟模型协议、定位/补丁/合成反馈、恢复、结果摘要、可选通用压缩及按修复义务组织的工作上下文。新写 session/report/completion 10/7/4 冻结配置、主机来源和实际发送计划，context_profile 明确登记 format 2；历史 inline 及更旧记录保持原形状只读。run 内原字节材料与独立求解投影已通过容量切片，代表千文件路径在原默认预算策略下完成合成闭环。主机批准经验条目、知识快照和有界检索已有独立接口，查询可保存并从原件重算，尚未进入 SDK 求解或自动学习；下一包核对经验前提与当前证据的关系。SDK 隐式摘要与卸载关闭，Graph 原历史保留，所有模型用途共用预算；聚合和私有导出能核验既有发送材料，新知识协议尚未接入。真实适配仍待补齐，不表示 P1 或完整系统已完成。后续本机 Agent 拟通过云端大模型 API 接入基础模型；HarmonyOS 应用验证和效果评测在本机受管工作流进行。本地权重部署和小模型微调不在当前路线；具体模型尚未选定。当前仍只做设计和合成软件自检，不启动真实模型或应用实验。

## 已完成与证据

| 内容 | 实际状态 | 依据 |
|---|---|---|
| 数据与材料核查 | 2026-09-15 完成全量只读盘点；2026-09-28 复核 021/022 两个待接入候选的原日志字节、源码位置与场景脚本。完整运行版本、原故障复现和独立业务 oracle 仍未知；未生成正式任务清单 | [数据报告](preparation/data-b-assessment.md)、resources/preparation/data-b-inventory.json |
| 项目蓝图 | 已明确目标工具、案例流程、重点和技术底座概览；详细规格仍需闭合 | DEVELOPMENT.md、开发设计第 1.1 节 |
| 技术选型 | Deep Agents SDK（底层 LangGraph）/Pydantic/SQLite 已接入固定响应路径；HTTPX/FastAPI/Uvicorn 为已完成的模拟协议原型，不再是本机应用执行的必需服务。真实模型与本机设备执行未接入，应用修复未验证 | 开发设计第 1.1、6 节；uv.lock；W03/W05/W06 自检 |
| 方法设计 | 修复义务贯穿定位、补丁与验证的主线、同知识对照及近邻边界已明确。工作上下文已接实际 SDK 输入；经验来源权限、条目、知识快照和普通有界检索已有独立实现。三态适用性、Skill 演进与 pending 再验证仍待实现，晋升门槛待定。检索是共同工程基线，合成结构核验不证明语义或收益 | [设计第 7、8 节](development/system-design-draft.md#7-skills-的具体位置)、[模块规格第 3.1 节](design-contracts.md#31-工作上下文经验与-skill-的实施契约)、[检索记录](../sessions/2026-10-04-W07-knowledge-retrieval.json) |
| 本机资源与交接设计 | 已定义集中目录、资源限制、停止/清理协议及 W01–W10 实现包；Job 限制对合成 Run、Init、pytest、质量检查、离线 Agent、聚合及 Export 生效，真实模型/应用任务尚未接入 | 开发设计第 9 节、模块规格第 5 节、实施计划 |
| W01 本机管理 | Prepare/Status/Clean 已实现；清理默认预览并校验 owner、目标指纹、活动标记和链接；原批 37 条隔离合成断言通过，后续证据清理保护验收见 W10 | scripts/local.ps1、scripts/test_local_runtime.ps1、sessions/2026-09-23-W01-local-cleanup.json |
| W02 进程保护 | Run/Stop 已实现固定合成场景；Job 的 CPU/提交内存/进程数设置后读回，子进程挂起加入后运行；输出管道按字节上限收集，监测时间、内存预留和磁盘。25 条隔离断言通过 | scripts/local_job.cs、scripts/local_synthetic.ps1、scripts/test_local_process.ps1、sessions/2026-09-23-W02-local-processes.json |
| W03 受管依赖与 SDK 切片 | 项目专属 uv 0.12.9、CPython 3.13.5、uv.lock 已就位；Deep Agents 0.7.18 固定响应脚本验证编辑、候选登记、失败后修订、120 Token 记录、禁用委派与 SQLite 恢复。受管 Init 重复执行 10 项断言通过，SDK 合成测试 1 项通过；不代表真实模型或应用修复成功 | configs/bootstrap.json、uv.lock、tests/test_sdk_compat.py、scripts/test_local_bootstrap.ps1、sessions/2026-09-23-W03-managed-sdk.json |
| W04 首批领域对象与候选工作区 | 严格 Pydantic 对象及证据来源/行号/版本/引用检查；从已校验分析快照独立复制草稿，失败编辑原子回退，登记完整 diff、候选源码与哈希，检查前复核。合成测试 9 通过、1 项符号链接测试因当前账户权限跳过；硬链接隔离测试通过。尚非可构建运行工程 | src/dfx_agent/contracts.py、workspace.py；tests/test_contracts.py、test_workspace.py；sessions/2026-09-23-W04-contracts-workspace.json |
| W05 固定响应离线闭环 | Deep Agents 哈希校验日志读取、通用源码浏览/编辑工具、未确认诊断、候选登记门、合成检查反馈、调用/已知 Token/时间/候选预算与停止报告已接入；固定响应模型调用也通过独立 SQLite 账本，Graph 检查点前完成可回放，已发出未保存时保留未知且不重发。当前 12 项受管合成测试通过；受管 CLI repair/resume 冒烟为 9 月 29 日记录。仅合成文本判据，无应用成功 | src/dfx_agent/agent.py、models.py、model_calls.py、verification.py、cli.py；tests/test_agent_loop.py；.dfx-local/artifacts 中相应记录 |
| W05 任务策略与跨文件检索 | 显式 system_prompt 和主机任务上下文已接入 SDK；基础策略覆盖上游原始行定位、保行为补丁、检查和有依据的反馈修订，增强只按开关追加。search_source 可在核验原版/冻结候选上做分页字面检索，带版本/坐标/编码缺项，共用授权和总预算。57 项检索、19 项策略、9 项 SDK 交接测试通过，检索符号链接用例 1 项权限跳过；脚本跨文件定位→失败→再修订已打通。该批 session 8 冻结项目提示、初始上下文、工具定义和检索限额；尚无真实模型自主修复证据 | src/dfx_agent/task_strategy.py、source_search.py、agent.py；tests/test_task_strategy.py、test_source_search.py、test_task_handoff.py；sessions/2026-10-01-task-strategy-search.json |
| W05 模拟模型与结果交接 | 可配置 MockTransport 接入真实 SDK；唯一规范化发送体绑定消息/工具/参数与 profile，完整响应/原始 usage 保存，未知结果不重发，混合错误工具调用全批停止。该批主机 report 5 核验编辑前初次定位、最新候选/diff/check、决策/条件及调用/动作账本，标明不可回读诊断范围；该批 session 8 冻结 model_profile。受管 PowerShell repair/resume 模拟冒烟通过，正常结束后恢复新增请求为 0；无真实云端兼容或应用效果证据 | model_provider.py、models.py、reporting.py、offline_script.py、cli.py；新增测试；sessions/2026-10-02-model-boundary-report.json |
| W05 有限上下文与主机压缩 | 关闭 SDK 隐式摘要和消息/结果卸载；默认有限上下文，可选按完整工具组压缩模型发送视图。来源、摘要和实际 solver 请求独立冻结，所有用途共用预算；solver 序号与总量分开，恢复核对 Graph/账本。摘要先记费后验格式，unknown 不重发，已完成可在 Graph 前崩溃后回放；摘要不能授权工具、改写检查或写原版。54 项模块、13 项真实 SDK 合成交接及原上下文 24 项回归通过，包含末额度、无收益、产物篡改与 reserve 后链接失败；该批 session 8、report 5/completion 2。首版输入有限，摘要忠实度/真实效果未知 | context_compaction.py、models.py、agent.py、reporting.py、offline_script.py；tests/test_context_compaction.py、test_compaction_loop.py、test_context_boundary.py；sessions/2026-10-02-context-compaction.json |
| W05/W09 执行来源冻结 | 新 session 9/report 6/completion 3 保存受限源码原字节、项目/锁/资源配置、实际 Python/distribution 元数据及 Git 可用/未知观察；兼容摘要不含 Git，续跑保留初始记录。源码/锁/环境、旧会话或破损准入事实在可写账本/调用前拒绝；CLI 初始化拒绝也保留旧终态。旧 8/5/2 只读保持原摘要，新旧混合保留来源未知覆盖，已知兼容漂移拒绝。实际新运行→零新调用续跑→两格聚合→新包 Verify 通过，旧包摘要不变；Git 采集不可用，诚实保留 null。来源哈希/版本不是完整主机代码备份或二进制认证 | provenance.py、agent.py、reporting.py、metrics.py、cli.py、scripts/dfx.py、local_agent.ps1；四个 test_provenance 文件；[本批记录](../sessions/2026-10-03-execution-provenance.json) |
| W09 只读离线聚合 | 显式 task/variant/repeat 完整矩阵、配置/提示/工具重建核验、只读 SQLite 和源树前后检查已接入受管 aggregate；保存初始/最新定位、全部候选、首次/最终合成检查和完整成本。未运行/缺报告保留分母及未知费用，成对 repeat 按任务/声明谱系列出，不宣称独立性；应用指标与缺失历史版本保持 null。原批聚合、只读账本与入口合成验收及实际受管两格冒烟通过；后续导出见 W10 | metrics.py、evidence_io.py、local_metrics.ps1；tests/test_metrics.py、test_readonly_evidence.py、test_metrics_cli.py；sessions/2026-10-02-W09-offline-aggregation.json |
| W10 本机私有证据导出 | 受管 Export 的预览、冻结计划、复核复制、新目录发布及 Verify 已接入；包镜像原 task/work 路径，保留全部失败与未知项，从包内重聚合全文一致。Windows 长路径和 SQLite URI、实际工作进程祖先及新实例回读旧包已做合成验证；Graph 只做字节及数据库完整性检查，不执行。写入前复核清单/汇总大小与哈希，输入增长拒绝；源输入保留，导出与环境共用磁盘预算，终态 work、任务快照和未发布暂存仍需明确丢弃才清理。导出模块 64、入口 40、清理保护 1 项测试通过，实际受管预览/Apply/Verify 冒烟通过 | exporting.py、local_export.ps1、local.ps1；tests/test_exporting.py、test_export_cli.py、test_cleanup.py；[本批记录](../sessions/2026-10-02-W10-private-export.json) |
| W06 HTTP 模拟作业协议 | 版本/协议绑定请求、HTTPX 客户端意图、SQLite 唯一幂等键、查询、取消待确认、重启不确定状态及双端产物哈希校验已实现。FastAPI 进程内模拟服务 7 项合成测试通过，无后台进程或设备；所有结果为 synthetic/not_run。该原型可复用状态语义，不是本机真实执行适配器 | src/dfx_agent/backends.py、remote_service.py；tests/test_remote_contract.py；uv.lock；.dfx-local/artifacts 中对应记录 |
| W07 领域策略合成分支 | 四类规则卡、可关闭的规则/义务/查证开关、修法等价直接路径、区分力/能力/预算判断、候选义务与合成失败反例引用已接入固定响应 Agent 和受管固定脚本；基础与增强通用工具一致。10 项策略合成测试通过；模型推断关联不作硬拒绝，未运行实际诊断探针或应用 | src/dfx_agent/policies.py、agent.py、cli.py、workspace.py；tests/test_policies.py；.dfx-local/artifacts 中本批记录 |
| W07 独立工作上下文包 | 显式焦点取得诊断/义务/候选/反例必要闭包，核验来源、全局 ID、对象及原字节；推断和语义相关性保持未知。独立 packet 的同源重建不认证最新焦点，相关检查合法进展会使旧包重建拒绝；SDK 可见性及历史恢复由下行发送计划承担 | working_context.py；tests/test_working_context.py；[独立包记录](../sessions/2026-10-03-W07-working-context.json) |
| W07 工作上下文发送计划 | 可见授权回执决定焦点/水位，必要包和未表达领域组附入启动消息，近期及完整组按共同预算选择，再组合可选压缩。每个位点冻结原字节与实际发送，历史回读不从最新检查重建；fixed/mock 请求、账本、报告和导出共同核验。评测清单 2 显式比较开关，其余参数不放宽；历史 inline 保持原形状只读，当前材料与投影见下行 | working_context_plan.py、models.py、reporting.py、metrics.py、exporting.py；[发送交接记录](../sessions/2026-10-03-W07-context-send.json) |
| W07 大工程材料与求解投影 | plan 2 引用同 run 原字节 blob，保留完整审计 packet；求解投影仅替换 sources 回执清单，覆盖依据实际全文，其他领域内容原样保留。6.4 MB 源输入可进入实际 SDK 请求，超过原单 blob 上限仍在 reserve 前拒绝。1,201 源文件及候选在独立声明的 300 秒压力预算下完成合成修复/检查；审计 packet 超 131,072 字节、每投影低于 20,000 字节，重复引用按摘要复用。默认 60 秒初测 wall_time 失败保留，容量通过不代表效率达标。崩溃残留/unknown/后续检查/通用压缩、三代旧包只读核验通过 | working_context_materials.py、working_context_projection.py、working_context_plan.py；[本批记录](../sessions/2026-10-03-W07-context-materials.json) |
| W07 规模核验耗时 | 单操作去重入库、已核验任务内部延续、材料直接子文件闭合批量读、整库审计与该次回执共享原字节；读取请求按观察大小加增长哨兵，原限额不变。同一显式求解任务的单次 profiler on 从 165.59 降至 67.90 秒；off 从 23.84 至 21.43 秒。最终无插桩重复/不同内容的 1,201 文件任务均在冻结 max_seconds=60 策略下完成五次固定调用及合成检查，总调用 51.34/65.67 秒；初始化与报告核验不全在该预算计时内，不能说全调用都低于 60 秒。分析器只覆盖当前线程、read_bytes 是部分 I/O 代理；测试准备、顺序和缓存影响未独立控制，单次结果不证明稳定速度或实际修复收益。中间超时、Windows 临时目录重命名拒绝及测试准备换行错误原样保留，未提高预算或改变成功断言 | tests/test_working_context_scale.py；[本批记录](../sessions/2026-10-03-W07-verification-latency.json) |
| W07 经验条目与知识快照 | 已实现独立主机批准库：完整指导含前提/未知问题/步骤/观察分支/保留行为/反例/退出条件；Draft 仅引用批准材料，入库与快照核对全部来源权限交集及已声明历史谱系。原字节、条目、投影和检索声明物化冻结，旧 registry/快照不随新材料变更。构造/回读零创建，闭合回读核验祖先、FD 及恢复 mtime 的同长度改写；单记录、来源总量、引用集合和选择数量有门限。原批多 registry 累计容量缺口已修复，契约 124、存储 85 项合成测试通过；检索复用同操作闭包见下行。历史仅为主机 fixture 声明，应用资格无、成本非真实账本；未接三态、SDK、学习、会话/导出或 Skill 发布，哈希不认证自然语言或真实报告 | knowledge_contracts.py、knowledge.py；tests/test_knowledge_contracts.py、test_knowledge_store.py；[基础记录](../sessions/2026-10-04-W07-knowledge-snapshot.json) |
| W07 冻结经验检索 | 每快照显式建立 SQLite 派生索引；查询只用获准指导实际值，规范标签/去重词计分，零分/未知标签保留，稳定排序分页并返回完整投影。query 纯读，record 显式保存，read_record 从原件重算全文；只允许回读实际命中页。scope 在索引前核对，保存查询范围不认证调用者当前任务；原闭包、索引全部行、结构/页/附属文件和精确容量均核验，累计读与每连接 SQL 有门。独立审查的 64 位评分与 sidecar 路径先验缺口已修；新增契约 97、检索 97 项最终合成测试通过。未接 SDK/三态/模型/学习，排序不证明适用性、创新或提速 | retrieval_contracts.py、knowledge_retrieval.py；tests/test_retrieval_contracts.py、test_knowledge_retrieval.py；[本批记录](../sessions/2026-10-04-W07-knowledge-retrieval.json) |
| P0 动作与共同接受契约 | 模型动作提案与主机引用/能力/预算授权、候选无关的四层共同协议、故障签名、源码/运行版本绑定和保守接受门已形成 Pydantic 契约；13 项合成测试通过。动作授权已接入固定响应工具边界，接受门仍未接入真实打包器、业务 oracle 或设备；模拟或缺项不能成功 | src/dfx_agent/contracts.py、acceptance.py；tests/test_action_acceptance.py；模块规格第 1.2–1.3 节 |
| P0 逐工具授权与结果恢复 | SDK 当前 14 个项目工具在执行前校验参数、动作/能力、任务/版本、实际引用、候选与预算，绑定已记账的主机模型调用身份；SQLite 保存授权与工具结果，完成回放不重复动作，回执未存保持未知。有限条件与源码检索工具纳入同一能力和总工具预算，拒绝额外字段及隐藏嵌套请求的实际引用；15 项授权反例通过。固定响应和模拟请求路径共用授权；真实云端仍待接入 | src/dfx_agent/actions.py、agent.py、models.py；tests/test_tool_authorization.py；sessions/2026-10-01-condition-handoff.json |
| P0 因果诊断与修复义务交接 | 已补齐必要条件、值/状态绑定、因果步骤、支持/反驳引用、区分问题和双向义务关联；结构化提交由主机读取源码/日志、核对实际失败检查的候选版本，再原子保存诊断包。schema/既有记录可按需回读；候选和合成反馈共享义务 ID。21 项新增合成测试通过，引用契约 5 项、闭环 12 项、授权 15 项和策略 10 项回归通过。模型状态保持 unknown，此接纳入口仍不自动计算条件，因果真实性及效果未知 | src/dfx_agent/contracts.py、diagnostics.py、agent.py；tests/test_causal_diagnosis.py；sessions/2026-09-30-causal-diagnosis.json |
| P0 已登记诊断到修复决策 | DecisionRequest 绑定同任务/运行/源码的诊断、假设、证据、义务与区分问题；失败后仅使用已保存且匹配原候选的合成检查反馈，有限条件另声明完整期望范围并关联相关假设方向。保存建议与上下文，可回读并由候选成对引用决策/方向；原有直接/补查/未知/失败重审 21 项合成测试回归通过。session schema 8 拒绝旧会话续跑；仍不执行探针、不确认因果或硬拒绝推断义务 | src/dfx_agent/decisions.py、agent.py、actions.py；tests/test_decisions.py；sessions/2026-10-01-condition-handoff.json |
| P0 首批有限条件检查 | 索引范围、DataView 访问、TypedArray 容量/对齐/省略长度及使用点就绪状态已有程序接口，SDK 交接见下行。主机固定合成输入，核对任务/版本/诊断/绑定/对象/路径/采样点与单位；缺值或前提不足保留未知，不求值表达式、不改写假设或应用门。51 项计算合成测试通过；源码值提取和真实运行采样仍未实现 | src/dfx_agent/conditions.py；tests/test_conditions.py；模块规格第 3.0 节；sessions/2026-10-01-condition-handoff.json |
| P0 有限条件结果交接 | 主机输入及内容寻址结果不可改写保存；回读重核诊断、源码、候选、范围和完整计算。SDK 可按输入 ID 计算、按结果 ID 回读并提交同范围决策；violated 仅建议重审相关方向，satisfied/unknown 不确认修复，关闭关联保留未知。结果保存 23 项、交接/授权/恢复 30 项新增合成测试通过，原条件 51 项及授权 15 项、决策 21 项回归通过；报告 schema 5、会话 schema 8 冻结输入指纹。CLI 默认空 registry，程序测试可注入主机 fixture；可信采值及适用性仍未实现 | src/dfx_agent/conditions.py、agent.py、actions.py、decisions.py；tests/test_condition_store.py、test_condition_handoff.py；sessions/2026-10-01-condition-handoff.json |
| P0 可信采值与算子绑定设计 | 已在现有主文档定义操作/参数锚点、主机角色映射、来源分层、采值凭据、对象代次/操作事件、观察扰动、预算/恢复及 schema 迁移；逐项推演同单位角色置换、候选移位、使用前后状态、目标未到达和平台未知等反例，并做跨文档人工审查。独立合成入口见下行，真实采值与平台适用性仍待实现 | [设计第 3.2.1 节](development/system-design-draft.md#321-怎样取得可信的条件输入)、[规格第 3.0.1 节](design-contracts.md#301-可信采值与算子绑定)、实施计划；sessions/2026-10-01-value-source-design.json |
| P0 主机操作绑定与采值凭据合成契约 | 独立 SourceAnchor/BindingProposal/主机合成映射、CaptureIntent/Receipt 和不可改写登记已实现；实查 UTF-8 原字节、父操作、诊断、原版/候选和冻结 manifest。模拟事件核对操作、对象实例/代次/时序，回读不登记；追加 fixture 不改变旧凭据，来源始终 synthetic。66 项新增测试通过；工作区 4 通过/1 权限跳过、因果诊断 21、有限条件 51、条件保存 23、固定响应闭环 12 项回归通过。未接 SDK、新来源计算、解析器或设备 | src/dfx_agent/value_sources.py；tests/test_value_sources.py；sessions/2026-10-01-value-source-contracts.json |
| P0 案例选择契约 | 显式 schema 2 目录校验任务、案例键、谱系、变体、日志成员及可选场景脚本哈希，拒绝重复身份/同字节日志、不安全路径和会复制进分析快照的评测脚本；任务快照仅留下选择摘要。7 项合成测试通过。目录仍不能证明日志/源码同版或应用可运行 | src/dfx_agent/task.py、cli.py；tests/test_case_identity.py；数据报告候选审查 |
| P0 模型调用与恢复契约 | SQLite 请求账本在调用前登记并占用次数，发出后同 ID 不盲重试；完成响应可跨重启回放，格式错误和已知/缺失 usage 分开记录。records 按用途和归属只读列出完整状态，总预算仍覆盖全部用途。独立账本 21 项、固定响应 SDK 闭环 12 项合成测试通过。真实提供方、底层重试计费和实际 Token 硬限额仍未接入 | src/dfx_agent/model_calls.py、models.py；tests/test_model_calls.py、test_agent_loop.py；模块规格第 1.2.2 节 |
| 离线证据入口 | prepare/analyze 提供快照、日志解析、栈位置映射和证据报告，结果不自动确认根因或有效修复 | cli.py、logs.py、sources.py；修复循环见 W05 |
| 软件自检 | 本批所选 5 个文件分次受管验收，合计 415 通过、0 跳过：新增查询契约/检索各 97 项，经验契约 124、存储 85、SDK 闭环 12 项回归通过。Ruff/Pyright 通过；初次超长 pytest 参数 ID 触发 Windows 环境变量长度错误，改短 ID 后保留原超限断言；中间导入质量问题均修复并复验，失败记录保留。不是全仓同轮结果，不代表模型或应用效果；本批不重复旧包导出验证，新知识协议仍未进入导出 | .dfx-local/artifacts；[本批记录](../sessions/2026-10-04-W07-knowledge-retrieval.json)；[前批证据](../sessions/2026-10-04-W07-knowledge-snapshot.json) |
| 仓库管理 | 本地 Git 已建立，模板与开发材料分开；失效规划已归档，当前文档设有归属及检查规则 | AGENTS.md、DEVELOPMENT.md、Git 历史 |
| 文档自检 | 14 份登记文档由受管检查核验，目录登记仍只有 DEVELOPMENT 一份；实现与主文档经过人工及独立审查。检查器原批 5 个合成场景通过；7 个归档文件不再跟踪，3 个提交模板未改动；本批检查回执见 session | scripts/check_docs.py；[本批记录](../sessions/2026-10-04-W07-knowledge-retrieval.json)；sessions/2026-09-16-document-governance.json |

## 尚未完成

三态适用性交接、Skills 目录和候选演进；知识快照/检索记录与任务/运行权限、SDK 实际发送、会话/报告/导出及 ResearchProfile/学习因素评测的集成；真实经验提炼、完整派生来源闭包与实际报告/调用成本核验，代表规模经验库的性能与检索质量。当前基础依赖主机审查完整投影和声明来源，不报告经验迁移或学习收益。材料容量和两类代表千文件任务已有合成验收，全部 data_B 工程的真实耗时与运行能力仍未知；只读源码盘点和合成压力任务不等于其真实 Agent/应用实验。静态 RuleCard、任务内检查点和通用压缩继续可用，真实晋升策略数值仍待开发条件确定。材料、权限和可靠检索是工程基础，方法贡献仍须在同模型、同知识、同预算下验证义务组织与有条件经验迁移的效果。

正式应用聚合、可重建主机材料/应用工具链来源和可公开脱敏材料；摘要忠实度、提供方 Token 窗口/压缩配置和实际收益，超摘要输入后的滚动历史能力；真实模型/应用任务的受管 Run；有限源码 literal 提取、可信运行值采集、实际语法/类型下的算子/角色/候选坐标与目标平台适用性核验、新来源接入计算/SDK 及真实应用结果接纳；真实云端传输、提供方兼容性、底层重试和实际 Token 硬限额；真实模型自主根因定位和补丁效果；其余规则条件的程序检查及业务语义判定；完整运行工程与独立业务判据；本机 SDK/设备能力盘点、受管安装与本地作业适配器、设备状态核验；案例选择摘要与正式接受协议的可信绑定、正式任务清单、数据划分和真实效果评测。

真实模型修复运行、应用构建、设备回放次数均为 0。根因准确率、修复率和实际模型成本均未知。

## 环境与未决条件

- 用户已决定在本机进行后续应用验证与评测；当前本机 HarmonyOS 工具链/设备是否可用、容量和受管安装条件均未核实。环境缺失只阻塞应用验收，不阻塞 P0/P1。
- 当前受管环境使用项目专属 Python 3.13.5 与锁定依赖。9 月 16 日只读检查未在 PATH 找到 hdc/ohpm/hvigorw，未接通模型提供方；这些是观察记录，不是对所有安装位置和账号的完整证明。
- 2026-10-04 W07 检索批未安装或下载依赖，使用既有 Python 标准库 SQLite 与受管环境。所选软件验收后 Status 观察到项目环境与导出合计约 306.3 MiB、活动任务为 none；未生成新私有导出包。环境、缓存及失败记录保留，测试临时目录由既有入口按归属清理；未更改资源限额、全局环境或三个提交模板。实际资源及验证记录见本批 session。
- 本机 SDK/设备/模拟器及磁盘/内存余量、云端模型提供方/型号与 API 预算、私仓数据外发边界、正式赛程和评测规则细节尚未确定。`configs/local-runtime.json` 仍禁止真实模型/应用实验和本机 SDK 安装，直到受管适配与资源门槛验收；不要把计划写成已配置事实。

## 下一步及完成条件

**下一包：W07 经验前提的三态交接与当前任务绑定。** 让检索到的相关经验经过当前证据核对，再给出可尝试步骤、冲突提示或查证问题；先实现独立来源/版本交接，再进入 SDK 与静态 Skill。当前不使用 API 凭据或启动真实模型/应用。

1. 按规格第 3.1.2 节落实主机当前 task/run、批准 snapshot/scope 与查询记录的绑定；ApplicabilityProposal/Review 引用完整经验前提、当前诊断/假设/候选和已登记依据，主机实查来源、版本与前提覆盖。模型语义判断保留 model_inference，不通过主机保存升级为事实。
2. 用独立合成材料验收 supported/contradicted/unknown 的下一动作，缺证据返回问题，同异常不同业务前提不能直接照搬；旧任务、其他候选或伪造引用拒绝，历史范围不能替代当前检查。保留相同完整投影给普通检索对照，关闭增强仍可走通用路径；保存和回读核验完整闭包，不引入自动学习或真实实验。
3. 后续接静态 Skill 和实际 SDK 发送，先迁移新增配置、调用用途及会话/报告/导出/评测，再实现候选演进。W08 按本机资源和用户条件接云端模型、构建/设备及独立业务验收；符号链接用例待具备权限的环境复验。
