# 当前状态

状态：当前进度。最后维护：2026-10-02。
职责：唯一维护已完成工作、当前任务、阻塞及下一步；项目蓝图与目录见 [开发导览](../DEVELOPMENT.md)，阶段定义见 [实施计划](project-work-plan.md)。

## 当前阶段

P0 设计闭合进行中，P1 已打通任务策略、跨文件检索、固定/模拟模型协议、定位/补丁/合成反馈、恢复、结果摘要及可选主机上下文压缩。SDK 隐式摘要及文件卸载保持关闭；主机压缩只调整发送视图，统一计费与恢复，完整 Graph 历史和原版保留。离线只读聚合已有合成入口，证据导出仍须补齐，不表示 P1 或完整系统已完成。后续本机 Agent 拟通过云端大模型 API 接入基础模型；HarmonyOS 应用验证和效果评测在本机受管工作流进行。本地权重部署和小模型微调不在当前路线；具体模型尚未选定。当前仍只做设计和合成软件自检，不启动真实模型或应用实验。

## 已完成与证据

| 内容 | 实际状态 | 依据 |
|---|---|---|
| 数据与材料核查 | 2026-09-15 完成全量只读盘点；2026-09-28 复核 021/022 两个待接入候选的原日志字节、源码位置与场景脚本。完整运行版本、原故障复现和独立业务 oracle 仍未知；未生成正式任务清单 | [数据报告](preparation/data-b-assessment.md)、resources/preparation/data-b-inventory.json |
| 项目蓝图 | 已明确目标工具、案例流程、重点和技术底座概览；详细规格仍需闭合 | DEVELOPMENT.md、开发设计第 1.1 节 |
| 技术选型 | Deep Agents SDK（底层 LangGraph）/Pydantic/SQLite 已接入固定响应路径；HTTPX/FastAPI/Uvicorn 为已完成的模拟协议原型，不再是本机应用执行的必需服务。真实模型与本机设备执行未接入，应用修复未验证 | 开发设计第 1.1、6 节；uv.lock；W03/W05/W06 自检 |
| 方法设计 | 已落实竞争力设计、四项工作与失败风险、端到端推演及强基线比较要求；因果诊断、义务、有效合成反例和同范围有限条件结果已绑定修复决策，动作授权与共同接受对象已有合成实现；可信采样、算子适用性及真实控制器/应用验证仍未落地 | [设计草案](development/system-design-draft.md)、[模块规格](design-contracts.md) |
| 本机资源与交接设计 | 已定义集中目录、资源限制、停止/清理协议及 W01–W10 实现包；Job 限制对合成 Run、Init、pytest、质量检查、离线 Agent 和聚合生效，真实模型/应用任务尚未接入 | 开发设计第 9 节、模块规格第 5 节、实施计划 |
| W01 本机管理 | Prepare/Status/Clean 已实现；清理默认预览并校验 owner、目标指纹、活动标记和链接；当前 37 条隔离合成断言通过 | scripts/local.ps1、scripts/test_local_runtime.ps1、sessions/2026-09-23-W01-local-cleanup.json |
| W02 进程保护 | Run/Stop 已实现固定合成场景；Job 的 CPU/提交内存/进程数设置后读回，子进程挂起加入后运行；输出管道按字节上限收集，监测时间、内存预留和磁盘。25 条隔离断言通过 | scripts/local_job.cs、scripts/local_synthetic.ps1、scripts/test_local_process.ps1、sessions/2026-09-23-W02-local-processes.json |
| W03 受管依赖与 SDK 切片 | 项目专属 uv 0.12.9、CPython 3.13.5、uv.lock 已就位；Deep Agents 0.7.18 固定响应脚本验证编辑、候选登记、失败后修订、120 Token 记录、禁用委派与 SQLite 恢复。受管 Init 重复执行 10 项断言通过，SDK 合成测试 1 项通过；不代表真实模型或应用修复成功 | configs/bootstrap.json、uv.lock、tests/test_sdk_compat.py、scripts/test_local_bootstrap.ps1、sessions/2026-09-23-W03-managed-sdk.json |
| W04 首批领域对象与候选工作区 | 严格 Pydantic 对象及证据来源/行号/版本/引用检查；从已校验分析快照独立复制草稿，失败编辑原子回退，登记完整 diff、候选源码与哈希，检查前复核。合成测试 9 通过、1 项符号链接测试因当前账户权限跳过；硬链接隔离测试通过。尚非可构建运行工程 | src/dfx_agent/contracts.py、workspace.py；tests/test_contracts.py、test_workspace.py；sessions/2026-09-23-W04-contracts-workspace.json |
| W05 固定响应离线闭环 | Deep Agents 哈希校验日志读取、通用源码浏览/编辑工具、未确认诊断、候选登记门、合成检查反馈、调用/已知 Token/时间/候选预算与停止报告已接入；固定响应模型调用也通过独立 SQLite 账本，Graph 检查点前完成可回放，已发出未保存时保留未知且不重发。当前 12 项受管合成测试通过；受管 CLI repair/resume 冒烟为 9 月 29 日记录。仅合成文本判据，无应用成功 | src/dfx_agent/agent.py、models.py、model_calls.py、verification.py、cli.py；tests/test_agent_loop.py；.dfx-local/artifacts 中相应记录 |
| W05 任务策略与跨文件检索 | 显式 system_prompt 和主机任务上下文已接入 SDK；基础策略覆盖上游原始行定位、保行为补丁、检查和有依据的反馈修订，增强只按开关追加。search_source 可在核验原版/冻结候选上做分页字面检索，带版本/坐标/编码缺项，共用授权和总预算。57 项检索、19 项策略、9 项 SDK 交接测试通过，检索符号链接用例 1 项权限跳过；脚本跨文件定位→失败→再修订已打通。session 8 冻结项目提示、初始上下文、工具定义和检索限额；尚无真实模型自主修复证据 | src/dfx_agent/task_strategy.py、source_search.py、agent.py；tests/test_task_strategy.py、test_source_search.py、test_task_handoff.py；sessions/2026-10-01-task-strategy-search.json |
| W05 模拟模型与结果交接 | 可配置 MockTransport 接入真实 SDK；唯一规范化发送体绑定消息/工具/参数与 profile，完整响应/原始 usage 保存，未知结果不重发，混合错误工具调用全批停止。主机 report 5 核验编辑前初次定位、最新候选/diff/check、决策/条件及调用/动作账本，标明不可回读诊断范围；session 8 冻结 model_profile。受管 PowerShell repair/resume 模拟冒烟通过，正常结束后恢复新增请求为 0；无真实云端兼容或应用效果证据 | model_provider.py、models.py、reporting.py、offline_script.py、cli.py；新增测试；sessions/2026-10-02-model-boundary-report.json |
| W05 有限上下文与主机压缩 | 关闭 SDK 隐式摘要和消息/结果卸载；默认有限上下文，可选按完整工具组压缩模型发送视图。来源、摘要和实际 solver 请求独立冻结，所有用途共用预算；solver 序号与总量分开，恢复核对 Graph/账本。摘要先记费后验格式，unknown 不重发，已完成可在 Graph 前崩溃后回放；摘要不能授权工具、改写检查或写原版。54 项模块、13 项真实 SDK 合成交接及原上下文 24 项回归通过，包含末额度、无收益、产物篡改与 reserve 后链接失败；session 8、report 5/completion 2。首版输入有限，摘要忠实度/真实效果未知 | context_compaction.py、models.py、agent.py、reporting.py、offline_script.py；tests/test_context_compaction.py、test_compaction_loop.py、test_context_boundary.py；sessions/2026-10-02-context-compaction.json |
| W09 只读离线聚合 | 显式 task/variant/repeat 完整矩阵、配置/提示/工具重建核验、只读 SQLite 和源树前后检查已接入受管 aggregate；保存初始/最新定位、全部候选、首次/最终合成检查和完整成本。未运行/缺报告保留分母及未知费用，成对 repeat 按任务/声明谱系列出，不宣称独立性；应用指标与缺失历史版本保持 null。聚合 64、只读账本 27、入口 11 项新增合成测试通过，4 项符号链接权限跳过；实际受管两格冒烟通过，Export 尚未实现 | metrics.py、evidence_io.py、local_metrics.ps1；tests/test_metrics.py、test_readonly_evidence.py、test_metrics_cli.py；sessions/2026-10-02-W09-offline-aggregation.json |
| W06 HTTP 模拟作业协议 | 版本/协议绑定请求、HTTPX 客户端意图、SQLite 唯一幂等键、查询、取消待确认、重启不确定状态及双端产物哈希校验已实现。FastAPI 进程内模拟服务 7 项合成测试通过，无后台进程或设备；所有结果为 synthetic/not_run。该原型可复用状态语义，不是本机真实执行适配器 | src/dfx_agent/backends.py、remote_service.py；tests/test_remote_contract.py；uv.lock；.dfx-local/artifacts 中对应记录 |
| W07 领域策略合成分支 | 四类规则卡、可关闭的规则/义务/查证开关、修法等价直接路径、区分力/能力/预算判断、候选义务与合成失败反例引用已接入固定响应 Agent 和受管固定脚本；基础与增强通用工具一致。10 项策略合成测试通过；模型推断关联不作硬拒绝，未运行实际诊断探针或应用 | src/dfx_agent/policies.py、agent.py、cli.py、workspace.py；tests/test_policies.py；.dfx-local/artifacts 中本批记录 |
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
| 软件自检 | 本批所选 10 个测试文件合计 272 通过、4 项符号链接测试因账户权限跳过，不是全仓同轮运行。新增只读账本 27、聚合 64、CLI/脚本入口 11；既有报告 45、模型账本 21、授权 18、压缩模块 54、SDK 压缩交接 13、固定闭环 12、离线 CLI 7 项回归通过。全部为合成自检，未运行 data_B 应用 | .dfx-local/artifacts；sessions/2026-10-02-W09-offline-aggregation.json |
| 仓库管理 | 本地 Git 已建立，模板与开发材料分开；失效规划已归档，当前文档设有归属及检查规则 | AGENTS.md、DEVELOPMENT.md、Git 历史 |
| 文档自检 | 14 份登记文档检查无错误；检查器 5 个合成场景通过；7 个归档文件保留且不再跟踪，3 个提交模板未改动 | scripts/check_docs.py、sessions/2026-09-16-document-governance.json |

## 尚未完成

受管证据导出、正式应用聚合和执行版本来源记录；摘要忠实度、提供方 Token 窗口/压缩配置和实际收益，超摘要输入后的滚动历史能力；真实模型/应用任务的受管 Run；有限源码 literal 提取、可信运行值采集、实际语法/类型下的算子/角色/候选坐标与目标平台适用性核验、新来源接入计算/SDK 及真实应用结果接纳；真实云端传输、提供方兼容性、底层重试和实际 Token 硬限额；真实模型自主根因定位和补丁效果；其余规则条件的程序检查及业务语义判定；完整运行工程与独立业务判据；本机 SDK/设备能力盘点、受管安装与本地作业适配器、设备状态核验；案例选择摘要与正式接受协议的可信绑定、正式任务清单、数据划分和真实效果评测。

真实模型修复运行、应用构建、设备回放次数均为 0。根因准确率、修复率和实际模型成本均未知。

## 环境与未决条件

- 用户已决定在本机进行后续应用验证与评测；当前本机 HarmonyOS 工具链/设备是否可用、容量和受管安装条件均未核实。环境缺失只阻塞应用验收，不阻塞 P0/P1。
- 当前受管环境使用项目专属 Python 3.13.5 与锁定依赖。9 月 16 日只读检查未在 PATH 找到 hdc/ohpm/hvigorw，未接通模型提供方；这些是观察记录，不是对所有安装位置和账号的完整证明。
- 2026-10-02 本批未安装或下载依赖，沿用受管环境；Ruff、Pyright 和登记文档检查通过。Status 观察到项目受管目录约 302.5 MiB、活动任务为 none，临时目录保留 4,415 字节合成冒烟输入；离线源记录及汇总保留在 work，尚未导出。没有改变全局环境、资源数值配置或三个提交模板。
- 本机 SDK/设备/模拟器及磁盘/内存余量、云端模型提供方/型号与 API 预算、私仓数据外发边界、正式赛程和评测规则细节尚未确定。`configs/local-runtime.json` 仍禁止真实模型/应用实验和本机 SDK 安装，直到受管适配与资源门槛验收；不要把计划写成已配置事实。

## 下一步及完成条件

**下一工作包：W10 受管证据导出。** 在现有本机入口为 runs/exports 提供停止状态下的归属、文件清单/哈希、输入保留与回读核验，允许从导出包重建既有离线摘要。当前仍不使用 API 凭据或启动真实模型；W09 应用比较和 W08 本机应用验证未开始。

1. 先明确每个包需要保留的任务快照、源码/候选、session、报告、调用/动作/检查/诊断/决策/条件/压缩证据和原聚合清单；只导出明确登记的范围，拒绝活动任务、链接、错版本、缺证据及目标覆盖。私有消息不默认外发，导出前须披露内容边界；不能只保存 agent-report.json。
2. 受管 Export 的完成与重新核验必须先于清理，停止不等于导出，导出不自动删除源证据。只在合成 fixture 上验收重建、篡改和清理保留反例，不安装 SDK 或启动应用实验。真实执行前补齐 Git/lock/SDK 等版本来源与运行前冻结清单，不能把当前版本倒填旧会话。
3. 可信采值继续按实施计划保留独立增强子包，不阻塞基础尝试或导出；形成正式案例前核对完整源码谱系、可构建工程和独立行为判据。真实实验按用户安排和 W08 门槛启动，缺环境不编造成绩；符号链接拒绝用例在具备权限的环境复验。
