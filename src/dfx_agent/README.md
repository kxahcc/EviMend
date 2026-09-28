# 离线证据与固定响应修复闭环

状态：当前使用说明。最后维护：2026-09-28。
职责：维护现有命令、输入格式和行为限制；项目进度见 [STATUS](../../docs/STATUS.md)。

当前命令行支持输入快照、日志解码、栈位置到源码映射及离线报告。固定响应 `repair/resume` 已把 Deep Agents SDK、领域对象、候选工作区、合成检查和可关闭策略工具接成受管闭环。策略提供四类规则提示、模型推断义务、候选引用、合成失败反例及“直接修/先查证”建议；不会自行运行诊断探针。它没有接入真实模型、可构建应用、设备或业务判据；合成 `offline_checked` 只表示测试替身通过，不能解释为应用修复成功。

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

`<...>` 是要替换的参数示意，不是可直接运行的真实任务。输入路径须在仓库内且不在 `data_B/` 或 `archive/`；工作目录固定为 `.dfx-local/work/offline-<ID>/`。脚本格式是 `{"schema_version":1,"responses":[{"tool":"edit_source","args":{"path":"src/main.ets","old":"bad","new":"good"},"usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15}}],"expected":{"path":"src/main.ets","text":"value=good\n"}}`；可选 `policy_features` 对象含 `rule_guidance`、`obligation_tracking`、`decision_checks` 三个布尔开关，缺省全关。`tool:null` 表示模型结束消息，`usage:null` 表示真实用量未知。这个 expectation 只用于合成替身，不得放入未来独立评测求解上下文。`agent-report.json` 记录候选、离线检查、调用、策略开关和未知 usage；`run-state.json` 记录受管工作进程终态；SQLite 和检查意图支持恢复。恢复沿用原 ID、预算和策略开关，检查重试沿用原幂等键。被中断且尚无终态的工作目录会阻止清理，须先核对再恢复或处置。

合成输入自检位于 `tests/test_offline_evidence.py`，显式案例身份与哈希拒绝反例位于 `tests/test_case_identity.py`；领域对象和候选目录反例位于 `tests/test_contracts.py`、`tests/test_workspace.py`，固定响应闭环、远程模拟协议、策略分支及接受契约分别位于 `tests/test_agent_loop.py`、`tests/test_remote_contract.py`、`tests/test_policies.py`、`tests/test_action_acceptance.py`。这些检查不读取或运行 data_B 中的应用，不调用真实模型或设备。当前设计与待实现功能见 [技术设计](../../docs/development/system-design-draft.md) 和 [模块契约](../../docs/design-contracts.md)。

`acceptance.py` 当前只供独立程序调用：准备者冻结与候选无关的共同协议，可信打包器绑定完整运行源码，执行端按检查 ID 回传原版和候选的结果，接受门对缺失、模拟、版本错配和行为失败返回 `not_verified`。这些适配器及业务 oracle 尚未接入；该模块的合成 `verified_under_protocol` 测试值不是本项目已修复应用的成绩。

`backends.py` 和 `remote_service.py` 另提供 HTTPX 客户端及 FastAPI 模拟服务的程序接口。`tests/test_remote_contract.py` 使用进程内 `TestClient`，显式推进模拟工作者状态，检查请求幂等、响应丢失后的恢复、取消待确认、产物哈希及离线行为；不启动常驻服务、真实设备或应用构建。模拟服务只返回 `scope=synthetic` 和 `application_verification=not_run`。真实远端地址、凭据、设备工作者及可构建工程尚未配置，不能把该协议自检当作应用验证。
