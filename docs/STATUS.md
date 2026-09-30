# 当前状态

状态：当前进度。最后维护：2026-10-01。
职责：唯一维护已完成工作、当前任务、阻塞及下一步；项目蓝图与目录见 [开发导览](../DEVELOPMENT.md)，阶段定义见 [实施计划](project-work-plan.md)。

## 当前阶段

P0 设计闭合进行中。已存在部分 P1 离线原型，但不表示完整设计或 P1 已完成。后续本机 Agent 拟通过云端大模型 API 接入基础模型；HarmonyOS 应用验证和效果评测在本机受管工作流进行，不再等待远程执行主机。本地权重部署和小模型微调不在当前路线；具体模型尚未选定。当前仍先完善设计，不启动真实模型或应用实验。

## 已完成与证据

| 内容 | 实际状态 | 依据 |
|---|---|---|
| 数据与材料核查 | 2026-09-15 完成全量只读盘点；2026-09-28 复核 021/022 两个待接入候选的原日志字节、源码位置与场景脚本。完整运行版本、原故障复现和独立业务 oracle 仍未知；未生成正式任务清单 | [数据报告](preparation/data-b-assessment.md)、resources/preparation/data-b-inventory.json |
| 项目蓝图 | 已明确目标工具、案例流程、重点和技术底座概览；详细规格仍需闭合 | DEVELOPMENT.md、开发设计第 1.1 节 |
| 技术选型 | Deep Agents SDK（底层 LangGraph）/Pydantic/SQLite 已接入固定响应路径；HTTPX/FastAPI/Uvicorn 为已完成的模拟协议原型，不再是本机应用执行的必需服务。真实模型与本机设备执行未接入，应用修复未验证 | 开发设计第 1.1、6 节；uv.lock；W03/W05/W06 自检 |
| 方法设计 | 已落实竞争力设计、四项工作与失败风险、端到端推演及强基线比较要求；因果诊断、义务、有效合成反例和同范围有限条件结果已绑定修复决策，动作授权与共同接受对象已有合成实现；可信采样、算子适用性及真实控制器/应用验证仍未落地 | [设计草案](development/system-design-draft.md)、[模块规格](design-contracts.md) |
| 本机资源与交接设计 | 已定义集中目录、资源限制、停止/清理协议及 W01–W10 实现包；Job 限制对合成 Run、Init、pytest、质量检查和固定响应离线 Agent 生效，真实模型/应用任务尚未接入 | 开发设计第 9 节、模块规格第 5 节、实施计划 |
| W01 本机管理 | Prepare/Status/Clean 已实现；清理默认预览并校验 owner、目标指纹、活动标记和链接；当前 37 条隔离合成断言通过 | scripts/local.ps1、scripts/test_local_runtime.ps1、sessions/2026-09-23-W01-local-cleanup.json |
| W02 进程保护 | Run/Stop 已实现固定合成场景；Job 的 CPU/提交内存/进程数设置后读回，子进程挂起加入后运行；输出管道按字节上限收集，监测时间、内存预留和磁盘。25 条隔离断言通过 | scripts/local_job.cs、scripts/local_synthetic.ps1、scripts/test_local_process.ps1、sessions/2026-09-23-W02-local-processes.json |
| W03 受管依赖与 SDK 切片 | 项目专属 uv 0.12.9、CPython 3.13.5、uv.lock 已就位；Deep Agents 0.7.18 固定响应脚本验证编辑、候选登记、失败后修订、120 Token 记录、禁用委派与 SQLite 恢复。受管 Init 重复执行 10 项断言通过，SDK 合成测试 1 项通过；不代表真实模型或应用修复成功 | configs/bootstrap.json、uv.lock、tests/test_sdk_compat.py、scripts/test_local_bootstrap.ps1、sessions/2026-09-23-W03-managed-sdk.json |
| W04 首批领域对象与候选工作区 | 严格 Pydantic 对象及证据来源/行号/版本/引用检查；从已校验分析快照独立复制草稿，失败编辑原子回退，登记完整 diff、候选源码与哈希，检查前复核。合成测试 9 通过、1 项符号链接测试因当前账户权限跳过；硬链接隔离测试通过。尚非可构建运行工程 | src/dfx_agent/contracts.py、workspace.py；tests/test_contracts.py、test_workspace.py；sessions/2026-09-23-W04-contracts-workspace.json |
| W05 固定响应离线闭环 | Deep Agents 哈希校验日志读取、通用源码浏览/编辑工具、未确认诊断、候选登记门、合成检查反馈、调用/已知 Token/时间/候选预算与停止报告已接入；固定响应模型调用也通过独立 SQLite 账本，Graph 检查点前完成可回放，已发出未保存时保留未知且不重发。当前 12 项受管合成测试通过；受管 CLI repair/resume 冒烟为 9 月 29 日记录。仅合成文本判据，无应用成功 | src/dfx_agent/agent.py、models.py、model_calls.py、verification.py、cli.py；tests/test_agent_loop.py；.dfx-local/artifacts 中相应记录 |
| W06 HTTP 模拟作业协议 | 版本/协议绑定请求、HTTPX 客户端意图、SQLite 唯一幂等键、查询、取消待确认、重启不确定状态及双端产物哈希校验已实现。FastAPI 进程内模拟服务 7 项合成测试通过，无后台进程或设备；所有结果为 synthetic/not_run。该原型可复用状态语义，不是本机真实执行适配器 | src/dfx_agent/backends.py、remote_service.py；tests/test_remote_contract.py；uv.lock；.dfx-local/artifacts 中对应记录 |
| W07 领域策略合成分支 | 四类规则卡、可关闭的规则/义务/查证开关、修法等价直接路径、区分力/能力/预算判断、候选义务与合成失败反例引用已接入固定响应 Agent 和受管固定脚本；基础与增强通用工具一致。10 项策略合成测试通过；模型推断关联不作硬拒绝，未运行实际诊断探针或应用 | src/dfx_agent/policies.py、agent.py、cli.py、workspace.py；tests/test_policies.py；.dfx-local/artifacts 中本批记录 |
| P0 动作与共同接受契约 | 模型动作提案与主机引用/能力/预算授权、候选无关的四层共同协议、故障签名、源码/运行版本绑定和保守接受门已形成 Pydantic 契约；13 项合成测试通过。动作授权已接入固定响应工具边界，接受门仍未接入真实打包器、业务 oracle 或设备；模拟或缺项不能成功 | src/dfx_agent/contracts.py、acceptance.py；tests/test_action_acceptance.py；模块规格第 1.2–1.3 节 |
| P0 逐工具授权与结果恢复 | SDK 当前 13 个项目工具在执行前校验参数、动作/能力、任务/版本、实际引用、候选与预算，绑定已记账的主机模型调用身份；SQLite 保存授权与工具结果，完成回放不重复动作，回执未存保持未知。有限条件工具纳入同一能力和总工具预算，拒绝额外字段及隐藏嵌套请求的实际引用；15 项授权反例通过。同步固定响应路径以外仍待接入 | src/dfx_agent/actions.py、agent.py、models.py；tests/test_tool_authorization.py；sessions/2026-10-01-condition-handoff.json |
| P0 因果诊断与修复义务交接 | 已补齐必要条件、值/状态绑定、因果步骤、支持/反驳引用、区分问题和双向义务关联；结构化提交由主机读取源码/日志、核对实际失败检查的候选版本，再原子保存诊断包。schema/既有记录可按需回读；候选和合成反馈共享义务 ID。21 项新增合成测试通过，引用契约 5 项、闭环 12 项、授权 15 项和策略 10 项回归通过。模型状态保持 unknown，此接纳入口仍不自动计算条件，因果真实性及效果未知 | src/dfx_agent/contracts.py、diagnostics.py、agent.py；tests/test_causal_diagnosis.py；sessions/2026-09-30-causal-diagnosis.json |
| P0 已登记诊断到修复决策 | DecisionRequest 绑定同任务/运行/源码的诊断、假设、证据、义务与区分问题；失败后仅使用已保存且匹配原候选的合成检查反馈，有限条件另声明完整期望范围并关联相关假设方向。保存建议与上下文，可回读并由候选成对引用决策/方向；原有直接/补查/未知/失败重审 21 项合成测试回归通过。session schema 5 拒绝旧会话续跑；仍不执行探针、不确认因果或硬拒绝推断义务 | src/dfx_agent/decisions.py、agent.py、actions.py；tests/test_decisions.py；sessions/2026-10-01-condition-handoff.json |
| P0 首批有限条件检查 | 索引范围、DataView 访问、TypedArray 容量/对齐/省略长度及使用点就绪状态已有程序接口，SDK 交接见下行。主机固定合成输入，核对任务/版本/诊断/绑定/对象/路径/采样点与单位；缺值或前提不足保留未知，不求值表达式、不改写假设或应用门。51 项计算合成测试通过；源码值提取和真实运行采样仍未实现 | src/dfx_agent/conditions.py；tests/test_conditions.py；模块规格第 3.0 节；sessions/2026-10-01-condition-handoff.json |
| P0 有限条件结果交接 | 主机输入及内容寻址结果不可改写保存；回读重核诊断、源码、候选、范围和完整计算。SDK 可按输入 ID 计算、按结果 ID 回读并提交同范围决策；violated 仅建议重审相关方向，satisfied/unknown 不确认修复，关闭关联保留未知。结果保存 23 项、交接/授权/恢复 30 项新增合成测试通过，原条件 51 项及授权 15 项、决策 21 项回归通过；报告 schema 3、会话 schema 5 冻结输入指纹。CLI 默认空 registry，程序测试可注入主机 fixture；可信采值及适用性仍未实现 | src/dfx_agent/conditions.py、agent.py、actions.py、decisions.py；tests/test_condition_store.py、test_condition_handoff.py；sessions/2026-10-01-condition-handoff.json |
| P0 可信采值与算子绑定设计 | 已在现有主文档定义操作/参数锚点、主机角色映射、来源分层、采值凭据、对象代次/操作事件、观察扰动、预算/恢复及 schema 迁移；逐项推演同单位角色置换、候选移位、使用前后状态、目标未到达和平台未知等反例。已做跨设计/规格人工审查；只有设计，无新增采值代码、解析器或运行验证，旧软件测试结果未刷新 | [设计第 3.2.1 节](development/system-design-draft.md#321-怎样取得可信的条件输入)、[规格第 3.0.1 节](design-contracts.md#301-可信采值与算子绑定)、实施计划；sessions/2026-10-01-value-source-design.json |
| P0 案例选择契约 | 显式 schema 2 目录校验任务、案例键、谱系、变体、日志成员及可选场景脚本哈希，拒绝重复身份/同字节日志、不安全路径和会复制进分析快照的评测脚本；任务快照仅留下选择摘要。7 项合成测试通过。目录仍不能证明日志/源码同版或应用可运行 | src/dfx_agent/task.py、cli.py；tests/test_case_identity.py；数据报告候选审查 |
| P0 模型调用与恢复契约 | SQLite 请求账本在调用前登记并占用次数，发出后同 ID 不盲重试；完成响应可跨重启回放，格式错误和已知/缺失 usage 分开记录。独立账本 8 项、固定响应 SDK 闭环 12 项合成测试通过。真实提供方、底层重试计费和实际 Token 硬限额仍未接入 | src/dfx_agent/model_calls.py、models.py；tests/test_model_calls.py、test_agent_loop.py；模块规格第 1.2.2 节 |
| 离线原型 | 快照、日志解析、栈位置映射和证据报告 | src/dfx_agent；不包含语义根因推理或自动修复 |
| 软件自检 | 受管 pytest：原型 10 项、案例身份 7 项、SDK 固定响应 1 项、W04 9 项、W05 12 项、W06 7 项、W07 10 项、P0 接受契约 13 项、模型账本 8 项、逐工具授权 15 项、因果诊断交接 21 项、修复决策交接 21 项、有限条件检查 51 项、条件保存 23 项和 Agent 交接 30 项通过，W04 符号链接测试 1 项跳过。W01/W02 反例测试见对应行；均仅为合成自检，非应用评测 | .dfx-local/artifacts 下相应运行记录 |
| 仓库管理 | 本地 Git 已建立，模板与开发材料分开；失效规划已归档，当前文档设有归属及检查规则 | AGENTS.md、DEVELOPMENT.md、Git 历史 |
| 文档自检 | 14 份登记文档检查无错误；检查器 5 个合成场景通过；7 个归档文件保留且不再跟踪，3 个提交模板未改动 | scripts/check_docs.py、sessions/2026-09-16-document-governance.json |

## 尚未完成

真实模型/应用任务的受管 Run 与证据导出；主机操作绑定/采值凭据契约与接纳门的代码实现、有限源码 literal 提取、可信运行值采集、算子/角色/候选坐标与目标平台适用性核验及真实应用结果接纳；模型账本与真实模型入口及其底层重试集成、实际请求哈希核对和 Token 硬限额；语义根因定位和补丁生成；其余规则条件的程序检查及业务语义判定；完整运行工程与独立业务判据；本机 SDK/设备能力盘点、受管安装与本地作业适配器、设备状态核验；案例选择摘要与正式接受协议的可信绑定、正式任务清单、数据划分和真实效果评测。

模型修复运行、应用构建、设备回放次数均为 0。根因准确率、修复率和实际模型成本均未知。

## 环境与未决条件

- 用户已决定在本机进行后续应用验证与评测；当前本机 HarmonyOS 工具链/设备是否可用、容量和受管安装条件均未核实。环境缺失只阻塞应用验收，不阻塞 P0/P1。
- 当前受管环境使用项目专属 Python 3.13.5 与锁定依赖。9 月 16 日只读检查未在 PATH 找到 hdc/ohpm/hvigorw，未接通模型提供方；这些是观察记录，不是对所有安装位置和账号的完整证明。
- 本机 SDK/设备/模拟器及磁盘/内存余量、云端模型提供方/型号与 API 预算、私仓数据外发边界、正式赛程和评测规则细节尚未确定。`configs/local-runtime.json` 仍禁止真实模型/应用实验和本机 SDK 安装，直到受管适配与资源门槛验收；不要把计划写成已配置事实。

## 下一步及完成条件

**下一工作包：P0 主机操作绑定与采值凭据合成契约。** 设计及反例已落入现有主文档，下一步实现独立对象、原字节锚点核对、主机合成接纳与不可改写登记；尚不写实际采样器、安装解析依赖或接入新来源到 SDK。完整步骤见 [实施计划](project-work-plan.md#可信采值契约的实现范围)。W08 仍须先完成本机资源/工具链盘点和运行保护验收。

1. 按规格第 3.0.1 节实现拟议 value_sources.py 与受管 test_value_sources.py，先完成“提议→主机合成绑定→合成凭据→登记/回读”的独立路径。核对合法任务/诊断/冻结版本和精确源码字节，保存来源/状态、角色/算子映射、事件身份和缺前提；拒绝模型自授来源、同单位角色/算子置换、改写凭据和跨对象/事件使用。合成 host proof 不冒充真正解析或执行，保留当前 ConditionStore/session 的原有行为；完成新测试及受影响检查后更新状态和目录职责。真实采值和新来源计算分别在后续子包验收。
2. 形成正式案例前继续核对 021/022 及替补候选的完整源码谱系、可构建工程和独立行为判据；不把 README、构造报告或旧基线直接当真实标签/验收。对 W04 符号链接拒绝场景在具备权限的环境复验。
3. 资源条件具备并按用户安排启动实验时再进入 W08；无资源时可先做独立合成评测聚合器，不编造效果数字。
