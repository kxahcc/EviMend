# 离线证据与固定响应修复闭环

状态：当前使用说明。最后维护：2026-09-30。
职责：维护现有命令、输入格式和行为限制；项目进度见 [STATUS](../../docs/STATUS.md)。

当前命令行支持输入快照、日志解码、栈位置到源码映射及离线报告。固定响应 `repair/resume` 已把 Deep Agents SDK、因果诊断与修复义务、候选工作区、模型/工具账本、合成检查和可关闭策略工具接成受管闭环。策略提供四类规则提示、候选引用、合成失败反例及“直接修/先查证”建议；不会自行运行诊断探针。当前没有真实模型、可构建应用、设备或业务判据；合成 `offline_checked` 只表示测试替身通过，不能解释为应用修复成功。

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

结构化诊断使用 `inspect_analysis(include_schema=True)` 读取主机身份、提交 schema 和接纳规则，再把序列化的 DiagnosisSubmission 传给 `record_diagnosis(diagnosis_json=...)`。它包含异常/根因位置、条件与绑定、因果步骤、支持/反驳引用、未知项及可选义务；源码和日志事实由主机读取，模型只能提交推断。义务需要开启 `obligation_tracking`，并和假设双向关联。简单 path/line/reason 路径仍可用，不能与 diagnosis_json 同传。

`diagnoses/<ID>.json` 原子保存诊断、证据和关联义务，不能改写；修订用新 ID。已有证据可按 ID 复用，依据闭包保留原来源。`inspect_analysis(diagnosis_id=...)` 回读同一记录，默认不重复返回 schema。独立 `record_obligation` 也使用规范 RepairObligation；候选的 restores/preserves 引用上述义务。模型假设、关系和义务保持 unknown/model_inference；失败执行依据须匹配已保存检查的候选、实际源码哈希和 execution_id，不能把旧候选反馈移到新候选。完整字段与未实现检查见 [模块规格第 1 节](../../docs/design-contracts.md#1-数据契约与不可破坏的条件)。

`plan_repair_action(plan_json=...)` 现在接收序列化的 DecisionRequest：task_id、run_id、diagnosis_id 和 plan（PolicyInput）。方向必须引用该诊断的 hypothesis_ids、support_evidence_ids 和相关 obligation_ids；检查映射须与已登记区分问题对应。include_schema=True 同时返回 decision_request_schema。主机覆盖自报能力和预算，只建议直接修、已有证据补查或带未知尝试，不执行探针。单独的旧 PolicyInput JSON 不再作为此工具输入。

失败后 `link_failed_check` 返回反馈及 feedback_id，再决策时成对传该 feedback_id 与原 candidate_id；主机重新核对已保存失败检查、候选源码哈希和原观察。`decisions/<ID>.json` 保存建议及上下文摘要；`inspect_analysis(decision_id=...)` 可回读，默认列出决策和反馈 ID。`register_candidate` 可选成对传 decision_id/direction_id，核对同诊断、所选提议方向及声明义务；建议不强迫模型采用某一修法。失败与义务的关联仍是推断，只建议重审相关方向。

`agent-report.json` 的 schema 2 记录候选、离线检查、已保存决策、模型与工具调用、授权/拒绝、结果哈希、策略开关和未知状态；`run-state.json` 记录受管工作进程终态。`model-calls.sqlite` 保存请求/响应账本，`tool-actions.sqlite` 保存授权与完整工具结果，Graph 检查点和检查意图共同支持恢复。已完成工具在检查点落盘前中断时只回放结果；工具执行过但结果未保存时停止为 `action_result_unknown`，不盲目重做。恢复沿用原 ID、预算、主机能力和策略开关；`session.json` 使用 schema 4，对应当前工具定义和决策格式，旧 schema 1/2/3 会话拒绝续跑，须保留旧证据并另建工作 ID。被中断且尚无终态的工作目录会阻止清理，须先核对再恢复或处置。

合成输入自检位于 `tests/test_offline_evidence.py`，显式案例身份与哈希拒绝反例位于 `tests/test_case_identity.py`；领域对象和候选目录反例位于 `tests/test_contracts.py`、`tests/test_workspace.py`，固定响应闭环、HTTP 模拟协议、策略分支、接受契约、模型请求账本、逐工具授权/恢复、因果记录和修复决策交接分别位于 `tests/test_agent_loop.py`、`tests/test_remote_contract.py`、`tests/test_policies.py`、`tests/test_action_acceptance.py`、`tests/test_model_calls.py`、`tests/test_tool_authorization.py`、`tests/test_causal_diagnosis.py`、`tests/test_decisions.py`。这些检查不读取或运行 data_B 中的应用，不调用真实模型或设备。当前设计与待实现功能见 [技术设计](../../docs/development/system-design-draft.md) 和 [模块契约](../../docs/design-contracts.md)。

模型账本的程序接口是 `ModelCallIntent`、`ModelCallStore` 和 `invoke_accounted`：调用前持久登记并占用次数，发出后若丢失响应则保留未知状态，同一 ID 不重发；收到的 `AIMessage` 保存为可恢复响应，格式错误与 Token usage 分开记账。固定响应模型根据实际传给 SDK 的消息、工具定义和参数生成哈希，完成响应在 Graph 未落检查点时可回放；请求已发出但无响应时报告 `model_result_unknown` 并停止。真实提供方适配器仍须核对实际发送请求、禁用或逐次登记内部重试；当前 CLI 不具备真实模型计费能力。

`acceptance.py` 当前只供独立程序调用：准备者冻结与候选无关的共同协议，可信打包器绑定完整运行源码，执行端按检查 ID 回传原版和候选的结果，接受门对缺失、模拟、版本错配和行为失败返回 `not_verified`。这些适配器及业务 oracle 尚未接入；该模块的合成 `verified_under_protocol` 测试值不是本项目已修复应用的成绩。

`backends.py` 和 `remote_service.py` 另提供 HTTPX 客户端及 FastAPI 模拟服务的程序接口。`tests/test_remote_contract.py` 使用进程内 `TestClient`，显式推进模拟工作者状态，检查请求幂等、响应丢失后的恢复、取消待确认、产物哈希及离线行为；不启动常驻服务、真实设备或应用构建。模拟服务只返回 `scope=synthetic` 和 `application_verification=not_run`。后续真实应用执行改走本机受管作业适配器；其 SDK/设备能力与完整工程尚未配置，不能把 HTTP 协议自检当作应用验证。
