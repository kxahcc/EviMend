# 离线证据与固定响应修复闭环

状态：当前使用说明。最后维护：2026-10-01。
职责：维护现有命令、输入格式和行为限制；项目进度见 [STATUS](../../docs/STATUS.md)。

当前命令行支持输入快照、日志解码、栈位置到源码映射及离线报告。固定响应 `repair/resume` 已把 Deep Agents SDK、因果诊断与修复义务、有限条件结果、候选工作区、模型/工具账本、合成检查和可关闭策略工具接成受管闭环。策略提供四类规则提示、候选引用、合成失败反例及“直接修/先查证”建议；不会自行运行诊断探针。当前没有真实模型、可构建应用、设备或业务判据；合成 `offline_checked` 只表示测试替身通过，不能解释为应用修复成功。

从项目根目录查看命令：

```powershell
python scripts/dfx.py --help
python scripts/dfx.py prepare --help
python scripts/dfx.py analyze --help
.\scripts\local.ps1 Run -Task test -TestPath tests/test_agent_loop.py
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

固定响应离线演示必须通过受管入口执行，输入已准备并通过 `verify_task` 的任务快照、JSON 脚本和新的工作 ID：

```powershell
.\scripts\local.ps1 Run -Task offline -OfflineTaskPath <快照目录> -OfflineScriptPath <固定响应JSON> -OfflineWorkId demo1
.\scripts\local.ps1 Run -Task offline -OfflineTaskPath <快照目录> -OfflineScriptPath <固定响应JSON> -OfflineWorkId demo1 -Resume
```

`<...>` 是要替换的参数示意，不是可直接运行的真实任务。输入路径须在仓库内且不在 `data_B/` 或 `archive/`；工作目录固定为 `.dfx-local/work/offline-<ID>/`。脚本格式是 `{"schema_version":1,"responses":[{"tool":"edit_source","args":{"path":"src/main.ets","old":"bad","new":"good"},"usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15}}],"expected":{"path":"src/main.ets","text":"value=good\n"}}`；可选 `policy_features` 对象含 `rule_guidance`、`obligation_tracking`、`decision_checks` 三个布尔开关，缺省全关。`tool:null` 表示模型结束消息，`usage:null` 表示提供方用量未知。这个 expectation 只用于合成替身，不得放入未来独立评测求解上下文。

每次 SDK 工具调用先经 `actions.py` 的主机授权：检查工具对应动作、参数、任务/版本、引用、候选及能力；全部动作串行，并受独立工具预算约束。工具参数可带 `proposal_json`（序列化的 `ActionProposal`），用于显式提交理由和引用；它不能改变工具所需能力或候选，也不能隐藏工具实际引用。模型返回的工具调用并不自动获准；最后一个已记账模型响应仍可完成合法动作，但不能绕过工具、候选和时间预算。

`task_strategy.py` 已向 SDK 注入明确的任务指引：读日志、追查跨文件的上游值与消费者、提出原始行假设、生成保行为补丁、冻结候选、按实际检查反馈修订或诚实停止。基础/增强共享该流程，增强只按开关追加规则、义务和决策检查。启动消息绑定主机任务、版本、能力与合成验证级别；脚本响应自检能证明交接，不能证明模型自主定位或修复效果。

`search_source(query=..., path_prefix=..., candidate_id=..., offset=..., limit=...)` 可快速找符号定义、赋值与调用文本，默认搜索原版；指定已登记 candidate_id 可查冻结候选，不能查草稿。查询是单行字面文本，前缀是相对文件或目录、不展开 glob。结果有实际源码版本、路径、行列、原字节哈希与有限片段，分页由 has_more/next_offset 指示；complete/skipped 明确编码覆盖。片段截断不能代替完整上下文，缺编码文件时不能声称不存在匹配。整树/单文件/查询/页面限额来自 source_search.py，超过即拒绝；工具共用 read_source 能力和总预算。它只查文本，不确认数据流或使用点运行值。

结构化诊断使用 `inspect_analysis(include_schema=True)` 读取主机身份、提交 schema 和接纳规则，再把序列化的 DiagnosisSubmission 传给 `record_diagnosis(diagnosis_json=...)`。它包含异常/根因位置、条件与绑定、因果步骤、支持/反驳引用、未知项及可选义务；源码和日志事实由主机读取，模型只能提交推断。义务需要开启 `obligation_tracking`，并和假设双向关联。简单 path/line/reason 路径仍可用，不能与 diagnosis_json 同传。

`diagnoses/<ID>.json` 原子保存诊断、证据和关联义务，不能改写；修订用新 ID。已有证据可按 ID 复用，依据闭包保留原来源。`inspect_analysis(diagnosis_id=...)` 回读同一记录，默认不重复返回 schema。独立 `record_obligation` 也使用规范 RepairObligation；候选的 restores/preserves 引用上述义务。模型假设、关系和义务保持 unknown/model_inference；失败执行依据须匹配已保存检查的候选、实际源码哈希和 execution_id，不能把旧候选反馈移到新候选。完整字段与未实现检查见 [模块规格第 1 节](../../docs/design-contracts.md#1-数据契约与不可破坏的条件)。

`plan_repair_action(plan_json=...)` 现在接收序列化的 DecisionRequest：task_id、run_id、diagnosis_id 和 plan（PolicyInput）。方向必须引用该诊断的 hypothesis_ids、support_evidence_ids 和相关 obligation_ids；检查映射须与已登记区分问题对应。include_schema=True 同时返回 decision_request_schema。主机覆盖自报能力和预算，只建议直接修、已有证据补查或带未知尝试，不执行探针。单独的旧 PolicyInput JSON 不再作为此工具输入。

失败后 `link_failed_check` 返回反馈及 feedback_id，再决策时成对传该 feedback_id 与原 candidate_id；主机重新核对已保存失败检查、候选源码哈希和原观察。`decisions/<ID>.json` 保存建议及上下文摘要；`inspect_analysis(decision_id=...)` 可回读，默认列出决策和反馈 ID。`register_candidate` 可选成对传 decision_id/direction_id，核对同诊断、所选提议方向及声明义务；建议不强迫模型采用某一修法。失败与义务的关联仍是推断，只建议重审相关方向。

`agent-report.json` 的 schema 3 记录候选、离线检查、已保存决策、condition_results、模型与工具调用、授权/拒绝、结果哈希、策略开关和未知状态；`run-state.json` 记录受管工作进程终态。`model-calls.sqlite` 保存请求/响应账本，`tool-actions.sqlite` 保存授权与完整工具结果，Graph 检查点和检查意图共同支持恢复。已完成工具在检查点落盘前中断时只回放结果；工具执行过但回执未保存时停止为 `action_result_unknown`，不盲目重做。恢复沿用原 ID、预算、主机能力、策略开关、条件输入指纹、项目任务提示、初始上下文、工具定义和检索限额；`session.json` 使用 schema 6，对应当前任务策略、检索工具定义和条件引用格式，旧 schema 1–5 会话拒绝续跑，须保留旧证据并另建工作 ID。被中断且尚无终态的工作目录会阻止清理，须先核对再恢复或处置。

合成输入自检位于 `tests/test_offline_evidence.py`，显式案例身份与哈希拒绝反例位于 `tests/test_case_identity.py`；领域对象和候选目录反例位于 `tests/test_contracts.py`、`tests/test_workspace.py`，固定响应闭环、HTTP 模拟协议、策略分支、接受契约、模型请求账本、逐工具授权/恢复、因果记录和修复决策交接分别位于 `tests/test_agent_loop.py`、`tests/test_remote_contract.py`、`tests/test_policies.py`、`tests/test_action_acceptance.py`、`tests/test_model_calls.py`、`tests/test_tool_authorization.py`、`tests/test_causal_diagnosis.py`、`tests/test_decisions.py`。这些检查不读取或运行 data_B 中的应用，不调用真实模型或设备。当前设计与待实现功能见 [技术设计](../../docs/development/system-design-draft.md) 和 [模块契约](../../docs/design-contracts.md)。

模型账本的程序接口是 `ModelCallIntent`、`ModelCallStore` 和 `invoke_accounted`：调用前持久登记并占用次数，发出后若丢失响应则保留未知状态，同一 ID 不重发；收到的 `AIMessage` 保存为可恢复响应，格式错误与 Token usage 分开记账。固定响应模型根据实际传给 SDK 的消息、工具定义和参数生成哈希，完成响应在 Graph 未落检查点时可回放；请求已发出但无响应时报告 `model_result_unknown` 并停止。真实提供方适配器仍须核对实际发送请求、禁用或逐次登记内部重试；当前 CLI 不具备真实模型计费能力。

有限条件使用顺序是：登记含 RuntimeConstraint 的诊断，查看主机输入 ID 和范围，调用 evaluate_condition，再按结果 ID 回读或提交修复决策。工具接收 request_json 和可选 observation_id，不接受模型自填的值或结果；schema 可从 inspect_analysis(include_schema=True) 获取。ConditionRequest 引用诊断的假设和绑定，声明有限算子及完整范围。当前支持 array_index、data_view_access、typed_array_view 和 ready_at_use；不求值诊断表达式，不自动提取源码值或采集真实运行值。缺值、单位/范围/前提不符返回 unknown，非法目标或诊断引用拒绝。

主机合成测试可先建立 SyntheticObservations，再通过 `ConditionStore(workspace, registry).evaluate(request, observation_id)` 或 `run_fixed_agent(..., observations=registry)` 接入。registry 只由测试代码提供，保存为 condition-inputs.json；CLI 暂不开放注入文件参数，默认空 registry。省略 observation_id 得到 missing_observations 的 unknown，未登记 ID 拒绝。inspect_analysis 默认列出主机 fixture ID/范围和 condition_result_ids，传 condition_result_id 可回读完整记录。原有 ConditionEvaluator 的不保存接口仍可使用。

conditions/<result_id>.json 的 ConditionRecord 绑定结果内容、输入 registry 指纹及诊断包哈希；相同输入回读原记录。回读会重新核对源码/候选实际字节、诊断、输入和完整计算，拒绝改写结果、换输入或提高来源层级。恢复不重复计算登记动作；完整性核对仍会重算有限谓词。结果的 satisfied/violated 只表示固定合成输入的计算，applicability_authority 为 model_inference，根因确认保持 unknown、应用验证保持 not_run，不生成 CheckLedger 结果。

DecisionRequest 可增加 condition_result_ids 与完整 condition_scope，主机要求结果来自同诊断、同假设方向和完全相同范围。开启 obligation_tracking 后，violated 只产生 condition_review 的相关方向重审建议；satisfied/unknown 或关闭关联不确认修复、不消除未知，也不硬拒绝义务。基础/增强共享同一计算工具和总预算。详细前提见 [模块规格第 3.0 节](../../docs/design-contracts.md#30-首批有限条件检查)。边界、保存和交接分别自检 tests/test_conditions.py、tests/test_condition_store.py、tests/test_condition_handoff.py，均通过受管 Run -Task test -TestPath 执行，不运行 data_B 应用。

`value_sources.py` 提供独立的程序接口，不是 CLI 或 SDK 工具。由主机代码建立固定 profile 的 SyntheticValueAdapter，并提供 SyntheticBindingFixture；`ValueSourceStore(workspace, adapter).bind_operation(proposal, fixture_id)` 核对诊断、冻结版本和 UTF-8 原字节锚点。adapter.add_capture 追加固定 SyntheticCaptureFixture，`capture_values(request, fixture_id)` 先保存请求，再登记合成凭据；binding/intent/receipt/receipts 回读只做核验，不新建记录。工作目录内 value-sources/ 保存 manifest、bindings、requests 和 receipts；重开继续提供可信 adapter 与所需历史 fixture，不能只凭磁盘标签信任来源。请求不得自带值、角色映射、观测事件或确认状态。

这里的 source_literal/execution_sample 是模拟请求模式，输出来源始终 synthetic_fixture；没有真实语法/类型适配、literal 提取、设备动作或新条件结果，也不改变 ConditionStore 和 session schema。测试覆盖原字节/父操作、参数角色、对象实例/代次/时序、缺前提、不可改写和恢复反例，执行入口为 `.\scripts\local.ps1 Run -Task test -TestPath tests/test_value_sources.py`。完整职责及未来迁移见 [模块规格第 3.0.1 节](../../docs/design-contracts.md#301-可信采值与算子绑定)。

`acceptance.py` 当前只供独立程序调用：准备者冻结与候选无关的共同协议，可信打包器绑定完整运行源码，执行端按检查 ID 回传原版和候选的结果，接受门对缺失、模拟、版本错配和行为失败返回 `not_verified`。这些适配器及业务 oracle 尚未接入；该模块的合成 `verified_under_protocol` 测试值不是本项目已修复应用的成绩。

`backends.py` 和 `remote_service.py` 另提供 HTTPX 客户端及 FastAPI 模拟服务的程序接口。`tests/test_remote_contract.py` 使用进程内 `TestClient`，显式推进模拟工作者状态，检查请求幂等、响应丢失后的恢复、取消待确认、产物哈希及离线行为；不启动常驻服务、真实设备或应用构建。模拟服务只返回 `scope=synthetic` 和 `application_verification=not_run`。后续真实应用执行改走本机受管作业适配器；其 SDK/设备能力与完整工程尚未配置，不能把 HTTP 协议自检当作应用验证。
