# 离线证据与修复 Agent 使用说明

状态：当前使用说明。最后维护：2026-10-04。
职责：维护现有命令、输入格式和行为限制；项目进度见 [STATUS](../../docs/STATUS.md)。

当前命令行支持输入快照、日志解码、栈位置到源码映射及离线报告。`repair/resume` 支持固定响应和模拟模型协议，已把 Deep Agents SDK、因果诊断与修复义务、有限条件结果、候选工作区、模型/工具账本、合成检查和可关闭策略工具接成受管闭环；受管 aggregate 和 Export/Verify 可汇总并保存既有记录。策略提供四类规则提示、候选引用、合成失败反例及“直接修/先查证”建议；不会自行运行诊断探针。当前没有真实云端传输、可构建应用、设备或业务判据；合成 `offline_checked` 只表示测试替身通过，不能解释为应用修复成功。导出是未脱敏的本机私有包，正式应用汇总、执行版本来源和公开脱敏/提交仍待接入，实际验收见 STATUS。

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

脚本 schema 1/2 分别用于固定响应与模拟 wire；保持工作上下文关闭。新 schema 3/4 保持各自响应结构，必须另提供 `working_context`，例如 `{"enabled":true}`；其全部策略参数会展开冻结在 session，不能靠旧脚本的额外字段悄悄启用。schema 2/4 用于检查实际模型协议交接，仍是进程内模拟，不连接云端：

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

两种格式均可选 `policy_features`（rule_guidance、obligation_tracking、decision_checks 三个布尔开关，缺省全关）和 `limits`（max_calls、max_known_tokens、max_seconds、max_candidates、max_tool_calls，正数，未指定时沿用循环默认值）。文件最多 1 MiB，拒绝链接、额外字段、重复 JSON 键和非有限数值。配置不接受密钥、端点或真实网络模式；responses 与 expected 都不进入模型任务上下文，expected 只供合成执行器。模拟恢复按已完成调用数对齐响应流；完成请求回放不消费新响应，已发出但未知的请求不重发。已正常结束的 Graph 再 resume 只核对/汇总，不开启新模型对话；已用尽预算仍明确报告耗尽。修改模型配置或循环限额不能继续原会话。

每次 SDK 工具调用先经 `actions.py` 的主机授权：检查工具对应动作、参数、任务/版本、引用、候选及能力；全部动作串行，并受独立工具预算约束。工具参数可带 `proposal_json`（序列化的 `ActionProposal`），用于显式提交理由和引用；它不能改变工具所需能力或候选，也不能隐藏工具实际引用。模型返回的工具调用并不自动获准；最后一个已记账模型响应仍可完成合法动作，但不能绕过工具、候选和时间预算。

`task_strategy.py` 已向 SDK 注入明确的任务指引：读日志、追查跨文件的上游值与消费者、提出原始行假设、生成保行为补丁、冻结候选、按实际检查反馈修订或诚实停止。基础/增强共享该流程，增强只按开关追加规则、义务和决策检查。启动消息绑定主机任务、版本、能力与合成验证级别；脚本响应自检能证明交接，不能证明模型自主定位或修复效果。

`search_source(query=..., path_prefix=..., candidate_id=..., offset=..., limit=...)` 可快速找符号定义、赋值与调用文本，默认搜索原版；指定已登记 candidate_id 可查冻结候选，不能查草稿。查询是单行字面文本，前缀是相对文件或目录、不展开 glob。结果有实际源码版本、路径、行列、原字节哈希与有限片段，分页由 has_more/next_offset 指示；complete/skipped 明确编码覆盖。片段截断不能代替完整上下文，缺编码文件时不能声称不存在匹配。整树/单文件/查询/页面限额来自 source_search.py，超过即拒绝；工具共用 read_source 能力和总预算。它只查文本，不确认数据流或使用点运行值。

结构化诊断使用 `inspect_analysis(include_schema=True)` 读取主机身份、提交 schema 和接纳规则，再把序列化的 DiagnosisSubmission 传给 `record_diagnosis(diagnosis_json=...)`。它包含异常/根因位置、条件与绑定、因果步骤、支持/反驳引用、未知项及可选义务；源码和日志事实由主机读取，模型只能提交推断。义务需要开启 `obligation_tracking`，并和假设双向关联。简单 path/line/reason 路径仍可用，不能与 diagnosis_json 同传。

`diagnoses/<ID>.json` 原子保存诊断、证据和关联义务，不能改写；修订用新 ID。已有证据可按 ID 复用，依据闭包保留原来源。`inspect_analysis(diagnosis_id=...)` 回读同一记录，默认不重复返回 schema。独立 `record_obligation` 也使用规范 RepairObligation；候选的 restores/preserves 引用上述义务。模型假设、关系和义务保持 unknown/model_inference；失败执行依据须匹配已保存检查的候选、实际源码哈希和 execution_id，不能把旧候选反馈移到新候选。完整字段与未实现检查见 [模块规格第 1 节](../../docs/design-contracts.md#1-数据契约与不可破坏的条件)。

`plan_repair_action(plan_json=...)` 现在接收序列化的 DecisionRequest：task_id、run_id、diagnosis_id 和 plan（PolicyInput）。方向必须引用该诊断的 hypothesis_ids、support_evidence_ids 和相关 obligation_ids；检查映射须与已登记区分问题对应。include_schema=True 同时返回 decision_request_schema。主机覆盖自报能力和预算，只建议直接修、已有证据补查或带未知尝试，不执行探针。单独的旧 PolicyInput JSON 不再作为此工具输入。

失败后 `link_failed_check` 返回反馈及 feedback_id，再决策时成对传该 feedback_id 与原 candidate_id；主机重新核对已保存失败检查、候选源码哈希和原观察。`decisions/<ID>.json` 保存建议及上下文摘要；`inspect_analysis(decision_id=...)` 可回读，默认列出决策和反馈 ID。`register_candidate` 可选成对传 decision_id/direction_id，核对同诊断、所选提议方向及声明义务；建议不强迫模型采用某一修法。失败与义务的关联仍是推断，只建议重审相关方向。

`agent-report.json` 的 schema 7/completion 4 绑定 session 10 中保存的 execution_provenance，记录候选、离线检查、已保存决策、condition_results、模型与工具调用、授权/拒绝、结果哈希、策略开关和未知状态。旧 session 8/report 5/completion 2 与 9/6/3 保持原形状只读回读，拒绝跨版本拼接。摘要区分首次允许编辑前的初次定位、最新定位及最新候选关联定位；定位始终是原始源码坐标的模型推断。candidate_artifacts 列出完整候选、相对 patch_path、实际哈希和检查；latest_candidate_check_status 与历史通过列表分开，最新失败/未检查不会被旧通过覆盖。working_context_plans 记录可见动作水位、组织视图/实际发送哈希、选择范围与保留成本；context_compactions 记录摘要来源、响应与实际 solver 视图的核验状态，摘要不能覆盖检查事实。cost 记录完整调用分母及 calls_by_kind，缺失/无效/未知用量不补零；缺少计时依据的定位/修复耗时为 null，accepted 始终 false。

`reporting.read_completion(workspace, ledger, model_calls, actions)` 是程序回读接口：重新核对源码、补丁、诊断、模型/工具请求与回执、条件、决策和 session，拒绝与这些事实矛盾的摘要或展示字段；不重新调用模型或执行检查。report_validation 明列核验范围、用于重算的停止原因和无法回读重建的本轮计数/适配诊断，摘要哈希不代替独立来源可信性。报告文件与候选 diff 可直接审阅；批量回读使用下述 aggregate，保留完整核验依据使用受管私有 Export。

### 只读聚合已停止的合成记录

```powershell
.\scripts\local.ps1 Run -Task aggregate -EvaluationManifestPath <仓库相对清单.json> -EvaluationWorkId <新ID>
```

完整报告写入 `.dfx-local/work/aggregate-<ID>/aggregate-report.json`，终态写入同目录 run-state.json，控制台只打印小回执。任务沿用现有 Windows Job、串行入口及资源限制，不调用模型、执行工具/检查或读取固定脚本的答案。旧目录不覆盖，不支持 Resume；强制中断后若无终态，清理保持拒绝。格式与程序接口在 metrics.py，自检样例在 tests/test_metrics.py；指标及未知项口径见 [评测说明](../../docs/preparation/metrics-and-gates.md#当前离线聚合的口径)。

清单 schema 1 的必需字段：evaluation_id、scope=`offline_synthetic`、configuration、tasks、variants、repeat_ids、runs。tasks 列出 task_id/case_key/lineage_group_id/fault_variant_id、task_path、task.json 原字节 task_sha256、source_snapshot_sha256/crash_sha256/protocol_sha256/observations_sha256；task schema 2 还提供原 CaseSelection，task schema 1 selection=null。variants 每项为 variant_id 和完整 PolicyFeatures。runs 每项为 task_id/variant_id/repeat_id/run_id/work_path，必须覆盖全部矩阵；work_path=null 表示未运行。输入路径为相对于仓库的规范路径，不能指向 data_B、archive 或内部 Git/助手目录。

清单 schema 1 的 configuration 中 model_profile_sha256、context_profile_sha256 和 search_limits_sha256 分别取 `sha256(canonical(session[字段]))`，tool_schema_sha256 取 session 同名值，再列出共同 capabilities/max_calls/max_known_tokens/max_seconds/max_candidates/max_tool_calls。canonical/sha256 由 model_calls.py 提供；不要对随意缩进或不同排序的 JSON 直接取这些配置哈希。聚合会重算任务提示及实际主机/solver 工具定义，核对所有 session 与同组预算；旧普通 solver 原始请求体未保存，不声称完整历史请求重建；新运行启用工作上下文时另外核验保存的完整发送回执。

清单 schema 2 用于显式上下文对照：configuration 另给 `working_context={max_packet_bytes,max_view_bytes,retain_groups}`，variants 每项必有 `working_context_enabled`；这些限制仍共享。此时上下文配置哈希取 `sha256(canonical(metrics.shared_context_profile(session["context_profile"])))`，只将 enabled 归一为 false，保留压缩及其他参数。`parse_evaluation_manifest` 区分两代，不给旧清单倒填默认值。v1 可核验当前关闭工作上下文的运行，但不能接纳开启的运行。

新运行的 context_profile 冻结 working_context_format=2：plan schema 2 以路径、摘要和大小引用同 run 的 working-context/materials 原字节，重复材料只保存一次；完整审计 packet 仍保留逐文件来源。模型投影只将 sources 清单替换为有摘要的统计，保留其他领域内容全文，覆盖判断使用实际投影。材料/审计产物沿用 MAX_ARTIFACT_BYTES 单产物限额，WorkingContextPolicy.max_packet_bytes 限制求解投影；超限明确停止，不放大配置。旧 session 10 缺标记时按 format 1 只读核验原 inline 计划和原预算解释；旧报告/包摘要不倒填，来源不兼容不能续跑。

原材料先于计划发布，崩溃可留下未引用完整 blob 或有界 pending，审计只读保留并计数，不自动补引用或清理。单独 Plan/Pydantic 校验仅确认结构，外部原字节须经 WorkingContextStore 回读/绑定/审计核验。working_context_plans 的 materialized_bytes 是逻辑引用量，unique_referenced_material_bytes 按摘要去重；run_material_observation 是整个 run 的累计 namespace 观察，在各计划重复展示，不能求和为每调用开销。solver_projection_bytes、stage/final_bytes 是 UTF-8 字节，真实模型 usage 仍单独记账。规模自检不表示已适配 data_B 全部工程或有修复/Token 收益；实际边界与下一工作包见 STATUS。

缺目录、缺报告、失败和未知成本均保留在分母；损坏证据拒绝整批，不跳过。首次与最新定位/候选分开，repeat 与任务/谱系分开，不宣称独立样本。所有应用指标为 null、accepted=false。全旧 session 8 数据维持原汇总形状及历史版本 null；存在新来源时，逐行保留原记录，按 compatibility_sha256 列出来源与未知项，已知兼容身份不一致拒绝合并。缺报告的新来源仍须绑定匹配工作区和会话，不能据此补零费用或成功。从现有记录生成清单不能证明当时预登记，正式实验须先冻结清单与完整应用执行来源。程序 API 为 load_evaluation_manifest / aggregate_evaluation / write_evaluation_report；写入只允许新报告且不能与输入重叠。

`run-state.json` 记录受管工作进程终态。`model-calls.sqlite` 保存请求/响应账本，`tool-actions.sqlite` 保存授权与完整工具结果，Graph 检查点和检查意图共同支持恢复。已完成工具在检查点落盘前中断时只回放结果；工具执行过但回执未保存时停止为 `action_result_unknown`，不盲目重做。恢复沿用原 ID、预算、主机能力、策略开关、条件输入指纹、项目任务提示、初始上下文、工具定义、检索限额、model_profile 和 context_profile，以及新 session 10 的兼容来源身份。旧 schema 1–9 不续跑，不自动升级，须保留证据并另建工作 ID。来源准入拒绝时 CLI 返回 3，工作进程保留原终态；已准入后运行失败仍记录 failed。Graph 只保存连续 solver model-N，摘要使用独立身份，不能用总调用数恢复 solver 序号。被中断且尚无终态的工作目录会阻止清理，须先核对再恢复或处置。

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

模型账本的程序接口是 `ModelCallIntent`、`ModelCallStore` 和 `invoke_accounted`：调用前持久登记并占用次数，发出后若丢失响应则保留未知状态，同一 ID 不重发；收到的 `AIMessage` 保存为可恢复响应，格式错误与 Token usage 分开记账。records 按登记顺序、任务/运行和可选用途只读列出记录；usage_summary 始终计算全部用途。固定响应模型根据实际传给 SDK 的消息、工具定义和参数生成哈希；模拟请求模型以 PreparedRequest 的唯一规范化发送体固定消息/工具/参数、整个 body 和 profile 指纹，发送前再次核对。原始 usage 保留，合格计数另行归一化；格式错误的整个响应不执行工具，已知 Token 超限阻止下一次调用而非承诺实际硬限额。完成响应在 Graph 未落检查点时可回放；请求已发出但无响应时报告 model_result_unknown 并停止。当前只允许 MockTransport，真实提供方兼容性、摘要忠实度和真实计费仍须验证。

两种离线模型共享 context_profile：关闭 SDK 自动摘要和人类消息/工具结果卸载，避免它们写入冻结原版或绕过调用恢复。默认不压缩，序列化消息超限以 context_limit 停止，格式不支持以 context_format_unsupported 停止；字节限额定义在 models.py，与云端 Token 窗口不同。可选主机压缩由 context_compaction.py 的 CompactionPolicy 配置，保留原 Graph 全历史、system、启动任务和近期完整工具组，只替换模型发送视图中的较早闭合前缀。首版重新摘要全部前缀；摘要输入、输出或必须保留的尾部过大仍停止，不支持无限历史。

脚本顶层可添加 `"context_compaction":{"policy":{"enabled":true}}`；默认参数及约束以 CompactionPolicy 为准。固定响应 schema 1/3 还须按实际摘要顺序提供独立 `responses`，每项 `{"text":"归纳文字","usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15}}`；缺失列表在需要新摘要时停止。mock schema 2/4 禁止独立列表，摘要响应与 solver 响应在 model.responses 中按所有实际请求顺序交错。程序入口对应 `compaction_policy=CompactionPolicy(...)`，固定入口额外接受 compaction_responses；策略及提示指纹在 session 10 冻结，恢复不得替换。此配置仍不允许真实模型。

摘要使用无工具请求、独立 compaction-model-N 身份及统一预算，work/compactions 保存冻结原消息、有效摘要视图和 solver 绑定。摘要先保存 usage，再验证非空 text_only、输出限额和确实缩短；返回工具或无效归纳以 context_summary_invalid 停止，不重试、不执行摘要工具。结果未落 Graph 前恢复只回放已完成摘要，已发出未保存不重发；摘要耗尽额度后没有免费 solver。摘要包装的 authority 始终是 model_inference，“已通过”不能生成检查事实；reporting.read_completion 同时重建核验来源、响应及有效发送视图。合成验收用 test_context_compaction.py 与 test_compaction_loop.py，归纳质量和模型效果仍待实测。

有限条件使用顺序是：登记含 RuntimeConstraint 的诊断，查看主机输入 ID 和范围，调用 evaluate_condition，再按结果 ID 回读或提交修复决策。工具接收 request_json 和可选 observation_id，不接受模型自填的值或结果；schema 可从 inspect_analysis(include_schema=True) 获取。ConditionRequest 引用诊断的假设和绑定，声明有限算子及完整范围。当前支持 array_index、data_view_access、typed_array_view 和 ready_at_use；不求值诊断表达式，不自动提取源码值或采集真实运行值。缺值、单位/范围/前提不符返回 unknown，非法目标或诊断引用拒绝。

主机合成测试可先建立 SyntheticObservations，再通过 `ConditionStore(workspace, registry).evaluate(request, observation_id)` 或 `run_fixed_agent(..., observations=registry)` 接入。registry 只由测试代码提供，保存为 condition-inputs.json；CLI 暂不开放注入文件参数，默认空 registry。省略 observation_id 得到 missing_observations 的 unknown，未登记 ID 拒绝。inspect_analysis 默认列出主机 fixture ID/范围和 condition_result_ids，传 condition_result_id 可回读完整记录。原有 ConditionEvaluator 的不保存接口仍可使用。

conditions/<result_id>.json 的 ConditionRecord 绑定结果内容、输入 registry 指纹及诊断包哈希；相同输入回读原记录。回读会重新核对源码/候选实际字节、诊断、输入和完整计算，拒绝改写结果、换输入或提高来源层级。恢复不重复计算登记动作；完整性核对仍会重算有限谓词。结果的 satisfied/violated 只表示固定合成输入的计算，applicability_authority 为 model_inference，根因确认保持 unknown、应用验证保持 not_run，不生成 CheckLedger 结果。

DecisionRequest 可增加 condition_result_ids 与完整 condition_scope，主机要求结果来自同诊断、同假设方向和完全相同范围。开启 obligation_tracking 后，violated 只产生 condition_review 的相关方向重审建议；satisfied/unknown 或关闭关联不确认修复、不消除未知，也不硬拒绝义务。基础/增强共享同一计算工具和总预算。详细前提见 [模块规格第 3.0 节](../../docs/design-contracts.md#30-首批有限条件检查)。边界、保存和交接分别自检 tests/test_conditions.py、tests/test_condition_store.py、tests/test_condition_handoff.py，均通过受管 Run -Task test -TestPath 执行，不运行 data_B 应用。

`value_sources.py` 提供独立的程序接口，不是 CLI 或 SDK 工具。由主机代码建立固定 profile 的 SyntheticValueAdapter，并提供 SyntheticBindingFixture；`ValueSourceStore(workspace, adapter).bind_operation(proposal, fixture_id)` 核对诊断、冻结版本和 UTF-8 原字节锚点。adapter.add_capture 追加固定 SyntheticCaptureFixture，`capture_values(request, fixture_id)` 先保存请求，再登记合成凭据；binding/intent/receipt/receipts 回读只做核验，不新建记录。工作目录内 value-sources/ 保存 manifest、bindings、requests 和 receipts；重开继续提供可信 adapter 与所需历史 fixture，不能只凭磁盘标签信任来源。请求不得自带值、角色映射、观测事件或确认状态。

这里的 source_literal/execution_sample 是模拟请求模式，输出来源始终 synthetic_fixture；没有真实语法/类型适配、literal 提取、设备动作或新条件结果，也不改变 ConditionStore 和 session schema。测试覆盖原字节/父操作、参数角色、对象实例/代次/时序、缺前提、不可改写和恢复反例，执行入口为 `.\scripts\local.ps1 Run -Task test -TestPath tests/test_value_sources.py`。完整职责及未来迁移见 [模块规格第 3.0.1 节](../../docs/design-contracts.md#301-可信采值与算子绑定)。

`knowledge.py` 也只提供独立主机程序接口，没有 CLI、SDK 工具、SQLite 检索或自动学习。主机先审查完整 `SolverProjection` 文档和来源用途，准备 `HostMaterialManifest`；`HostMaterialRegistry(source_root, manifest)` 在只读构造时捕获批准原字节。然后在预建的受管目录中建立 `KnowledgeStore(root)`，以只含材料 ID 的 `ExperienceDraft` 调用 `admit_entry`，以明确条目和 `PermissionScope` 调用 `freeze_snapshot`。`read_entry`、`read_snapshot`、`read_projection(snapshot_sha256, entry_id)` 核验并回读具体内容，不新建文件、不读取最新别名、不补缺失材料。后续 SDK 调用还须绑定真实任务范围及主机选定快照。

store 内 `materials/` 保存原批准字节，`entries/` 保存内容及来源权限，`snapshots/` 绑定精确条目、投影、权限与检索声明；本批 Skill 列表为空。单记录、批准来源总量和一次引用集合的上限由 `knowledge_contracts.py` 定义，超限不截断；叶文件、祖先、读取时变化及闭合收尾均核验，采用串行主机读写协议。历史只接受主机合成描述，报告/产物哈希不等于实际报告认证，应用资格恒为无；历史范围和未知/失败会保留在指导中，原任务/候选/oracle 描述仅供审计。哈希不能判断自然语言是否含答案，完整指导须主机审核。原来源更新需新 registry，新条目或快照不会改写旧内容。字段、准入及未来协议迁移见 [规格第 3.1.2 节](../../docs/design-contracts.md#312-经验检索和适用性交接)。合成反例通过 `.\scripts\local.ps1 Run -Task test -TestPath tests/test_knowledge_contracts.py` 与相同入口的 `tests/test_knowledge_store.py` 执行。

`acceptance.py` 当前只供独立程序调用：准备者冻结与候选无关的共同协议，可信打包器绑定完整运行源码，执行端按检查 ID 回传原版和候选的结果，接受门对缺失、模拟、版本错配和行为失败返回 `not_verified`。这些适配器及业务 oracle 尚未接入；该模块的合成 `verified_under_protocol` 测试值不是本项目已修复应用的成绩。

`backends.py` 和 `remote_service.py` 另提供 HTTPX 客户端及 FastAPI 模拟服务的程序接口。`tests/test_remote_contract.py` 使用进程内 `TestClient`，显式推进模拟工作者状态，检查请求幂等、响应丢失后的恢复、取消待确认、产物哈希及离线行为；不启动常驻服务、真实设备或应用构建。模拟服务只返回 `scope=synthetic` 和 `application_verification=not_run`。后续真实应用执行改走本机受管作业适配器；其 SDK/设备能力与完整工程尚未配置，不能把 HTTP 协议自检当作应用验证。
