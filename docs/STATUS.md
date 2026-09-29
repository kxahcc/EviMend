# 当前状态

状态：当前进度。最后维护：2026-09-29。
职责：唯一维护已完成工作、当前任务、阻塞及下一步；项目蓝图与目录见 [开发导览](../DEVELOPMENT.md)，阶段定义见 [实施计划](project-work-plan.md)。

## 当前阶段

P0 设计闭合进行中。已存在部分 P1 离线原型，但不表示完整设计或 P1 已完成。用户安排先完善分析设计，远程 HarmonyOS 环境后续准备；暂不开展模型修复或应用运行实验。

## 已完成与证据

| 内容 | 实际状态 | 依据 |
|---|---|---|
| 数据与材料核查 | 2026-09-15 完成全量只读盘点；2026-09-28 复核 021/022 两个待接入候选的原日志字节、源码位置与场景脚本。完整运行版本、原故障复现和独立业务 oracle 仍未知；未生成正式任务清单 | [数据报告](preparation/data-b-assessment.md)、resources/preparation/data-b-inventory.json |
| 项目蓝图 | 已明确目标工具、案例流程、重点和技术底座概览；详细规格仍需闭合 | DEVELOPMENT.md、开发设计第 1.1 节 |
| 技术选型 | Deep Agents SDK（底层 LangGraph）/Pydantic/SQLite 与 HTTPX/FastAPI/Uvicorn 已锁定；固定响应循环和模拟 HTTP 协议已接入，真实模型与设备执行未接入，应用修复未验证 | 开发设计第 1.1、6 节；uv.lock；W03/W05/W06 自检 |
| 方法设计 | 已落实竞争力设计、四项工作与失败风险、端到端推演及强基线比较要求；首批领域、动作授权和共同接受对象已有合成契约，完整因果链及真实控制器/应用验证仍未落地 | [设计草案](development/system-design-draft.md)、[模块规格](design-contracts.md) |
| 本机资源与交接设计 | 已定义集中目录、资源限制、停止/清理协议及 W01–W10 实现包；Job 限制对合成 Run、Init、pytest、质量检查和固定响应离线 Agent 生效，真实模型/应用任务尚未接入 | 开发设计第 9 节、模块规格第 5 节、实施计划 |
| W01 本机管理 | Prepare/Status/Clean 已实现；清理默认预览并校验 owner、目标指纹、活动标记和链接；当前 37 条隔离合成断言通过 | scripts/local.ps1、scripts/test_local_runtime.ps1、sessions/2026-09-23-W01-local-cleanup.json |
| W02 进程保护 | Run/Stop 已实现固定合成场景；Job 的 CPU/提交内存/进程数设置后读回，子进程挂起加入后运行；输出管道按字节上限收集，监测时间、内存预留和磁盘。25 条隔离断言通过 | scripts/local_job.cs、scripts/local_synthetic.ps1、scripts/test_local_process.ps1、sessions/2026-09-23-W02-local-processes.json |
| W03 受管依赖与 SDK 切片 | 项目专属 uv 0.12.9、CPython 3.13.5、uv.lock 已就位；Deep Agents 0.7.18 固定响应脚本验证编辑、候选登记、失败后修订、120 Token 记录、禁用委派与 SQLite 恢复。受管 Init 重复执行 10 项断言通过，SDK 合成测试 1 项通过；不代表真实模型或应用修复成功 | configs/bootstrap.json、uv.lock、tests/test_sdk_compat.py、scripts/test_local_bootstrap.ps1、sessions/2026-09-23-W03-managed-sdk.json |
| W04 首批领域对象与候选工作区 | 严格 Pydantic 对象及证据来源/行号/版本/引用检查；从已校验分析快照独立复制草稿，失败编辑原子回退，登记完整 diff、候选源码与哈希，检查前复核。合成测试 9 通过、1 项符号链接测试因当前账户权限跳过；硬链接隔离测试通过。尚非可构建运行工程 | src/dfx_agent/contracts.py、workspace.py；tests/test_contracts.py、test_workspace.py；sessions/2026-09-23-W04-contracts-workspace.json |
| W05 固定响应离线闭环 | Deep Agents 哈希校验日志读取、通用源码浏览/编辑工具、未确认诊断、候选登记门、合成检查反馈、调用/已知 Token/时间/候选预算与停止报告已接入；固定响应模型调用也通过独立 SQLite 账本，Graph 检查点前完成可回放，已发出未保存时保留未知且不重发。12 项受管合成测试及本批受管 CLI repair/resume 冒烟通过。仅合成文本判据，无应用成功 | src/dfx_agent/agent.py、models.py、model_calls.py、verification.py、cli.py；tests/test_agent_loop.py；.dfx-local/artifacts 中相应记录 |
| W06 模拟远程作业协议 | 版本/协议绑定请求、HTTPX 客户端意图、SQLite 唯一幂等键、查询、取消待确认、重启不确定状态及双端产物哈希校验已实现。FastAPI 进程内模拟服务 7 项合成测试通过，无后台进程或设备；所有结果为 synthetic/not_run | src/dfx_agent/backends.py、remote_service.py；tests/test_remote_contract.py；uv.lock；.dfx-local/artifacts 中本批记录 |
| W07 领域策略合成分支 | 四类规则卡、可关闭的规则/义务/查证开关、修法等价直接路径、区分力/能力/预算判断、候选义务与合成失败反例引用已接入固定响应 Agent 和受管固定脚本；基础与增强通用工具一致。10 项策略合成测试通过；模型推断关联不作硬拒绝，未运行实际诊断探针或应用 | src/dfx_agent/policies.py、agent.py、cli.py、workspace.py；tests/test_policies.py；.dfx-local/artifacts 中本批记录 |
| P0 动作与共同接受契约 | 模型动作提案与主机引用/能力/预算授权、候选无关的四层共同协议、故障签名、源码/运行版本绑定和保守接受门已形成独立 Pydantic 契约；13 项合成测试通过。模拟结果、原故障未重现、业务/回归缺项和版本错配均不能得到成功。尚未接入 Agent 逐调用、真实打包器、业务 oracle 或设备 | src/dfx_agent/contracts.py、acceptance.py；tests/test_action_acceptance.py；模块规格第 1.2–1.3 节 |
| P0 案例选择契约 | 显式 schema 2 目录校验任务、案例键、谱系、变体、日志成员及可选场景脚本哈希，拒绝重复身份/同字节日志、不安全路径和会复制进分析快照的评测脚本；任务快照仅留下选择摘要。7 项合成测试通过。目录仍不能证明日志/源码同版或应用可运行 | src/dfx_agent/task.py、cli.py；tests/test_case_identity.py；数据报告候选审查 |
| P0 模型调用与恢复契约 | SQLite 请求账本在调用前登记并占用次数，发出后同 ID 不盲重试；完成响应可跨重启回放，格式错误和已知/缺失 usage 分开记录。独立账本 8 项、固定响应 SDK 闭环 12 项合成测试通过。真实提供方、底层重试计费和实际 Token 硬限额仍未接入 | src/dfx_agent/model_calls.py、models.py；tests/test_model_calls.py、test_agent_loop.py；模块规格第 1.2.2 节 |
| 离线原型 | 快照、日志解析、栈位置映射和证据报告 | src/dfx_agent；不包含语义根因推理或自动修复 |
| 软件自检 | 受管 pytest：原型 10 项、案例身份 7 项、SDK 固定响应 1 项、W04 9 项、W05 12 项、W06 7 项、W07 10 项、P0 接受契约 13 项、模型账本 8 项通过，W04 符号链接测试 1 项跳过。W01/W02 反例测试见对应行；均仅为合成自检，非应用评测 | .dfx-local/artifacts 下相应运行记录 |
| 仓库管理 | 本地 Git 已建立，模板与开发材料分开；失效规划已归档，当前文档设有归属及检查规则 | AGENTS.md、DEVELOPMENT.md、Git 历史 |
| 文档自检 | 14 份登记文档检查无错误；检查器 5 个合成场景通过；7 个归档文件保留且不再跟踪，3 个提交模板未改动 | scripts/check_docs.py、sessions/2026-09-16-document-governance.json |

## 尚未完成

真实模型/应用任务的受管 Run 与证据导出；动作契约逐工具调用接入、完整因果对象及真实应用结果接纳；模型账本与真实提供方及其底层重试集成、实际请求哈希核对和 Token 硬限额；语义根因定位和补丁生成；四类规则条件的实际程序检查及业务语义判定；完整运行工程与独立业务判据；模拟 HTTP 请求增加 check_id/场景绑定，真实远程工作者、设备状态核验及部署；案例选择摘要与正式接受协议的可信绑定、正式任务清单、数据划分和真实效果评测。

模型修复运行、应用构建、设备回放次数均为 0。根因准确率、修复率和实际模型成本均未知。

## 环境与未决条件

- 用户确认暂无 HarmonyOS 运行环境，将后续寻找远程资源。环境缺失只阻塞实际运行验收，不阻塞 P0/P1。
- 当前受管环境使用项目专属 Python 3.13.5 与锁定依赖。9 月 16 日只读检查未在 PATH 找到 hdc/ohpm/hvigorw，未接通模型提供方；这些是观察记录，不是对所有安装位置和账号的完整证明。
- 远程 SDK/设备/传输方式、模型资源与预算、正式赛程和评测规则细节尚未确定。不要把未决事项填成已配置事实。

## 下一步及完成条件

**下一工作包：P0 动作授权逐工具调用的合成接入。** 固定响应模型调用已用账本计数与恢复；独立动作/接受对象尚未进入实际工具控制器。W08 的真实模型及设备运行仍需用户后续安排与远程资源。

1. 先在固定响应 SDK 路径将模型工具调用映射到主机授权，工具拒绝不得产生执行事实；覆盖未知能力、错误版本/候选、预算耗尽及恢复重复调用。真实提供方适配时另核对实际请求哈希和内部重试，仍不用真实模型试跑。
2. 形成正式案例前继续核对 021/022 及替补候选的完整源码谱系、可构建工程和独立行为判据；不把 README、构造报告或旧基线直接当真实标签/验收。对 W04 符号链接拒绝场景在具备权限的环境复验。
3. 资源条件具备并按用户安排启动实验时再进入 W08；无资源时可先做独立合成评测聚合器，不编造效果数字。
