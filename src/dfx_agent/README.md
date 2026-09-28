# 离线证据与固定响应修复闭环

状态：当前使用说明。最后维护：2026-09-28。
职责：维护现有命令、输入格式和行为限制；项目进度见 [STATUS](../../docs/STATUS.md)。

当前命令行支持输入快照、日志解码、栈位置到源码映射及离线报告。固定响应 `repair/resume` 已把 Deep Agents SDK、领域对象、候选工作区和合成检查接成受管闭环。它没有接入真实模型、可构建应用、设备或业务判据；合成 `offline_checked` 只表示测试替身通过，不能解释为应用修复成功。

从项目根目录查看命令：

```powershell
python scripts/dfx.py --help
python scripts/dfx.py prepare --help
python scripts/dfx.py analyze --help
.\scripts\local.ps1 Run -Task test -TestPath tests/test_agent_loop.py
```

`prepare` 必须显式提供经过核验的 JSON catalog、case ID、数据根目录和新的输出目录。本阶段未选择真实任务，也未生成默认 catalog。catalog 的最小结构如下；这些是字段示例，不是可运行的真实路径或哈希：

```json
{
  "schema_version": 1,
  "cases": [{
    "task_id": "example",
    "app": "relative/application/directory",
    "log": {
      "path": "relative/crash.log",
      "sha256": "REPLACE_WITH_SHA256_OF_LOG_BYTES"
    }
  }]
}
```

ZIP 内日志还需要 `log.member`，哈希对应成员解压后的原始字节。`app` 和 `log.path` 均相对于 `--data-root`。输入源码过滤采用 `analysis_source_v1`；该快照会排除运行资源和测试材料，不是可构建的应用包，也不是操作系统访问隔离机制。

`analyze` 接收含 `task.json` 的快照目录，输出目录必须在该快照之外。报告保存栈位置、相关源码和映射歧义；`root_cause` 与 `patch` 为 null，验证状态保持 not_run。输入哈希校验能发现相对清单的改动，不证明清单的来源可信或日志与源码运行版本一致。

固定响应离线演示必须通过受管入口执行，输入已准备并通过 `verify_task` 的任务快照、JSON 脚本和新的工作 ID：

```powershell
.\scripts\local.ps1 Run -Task offline -OfflineTaskPath <快照目录> -OfflineScriptPath <固定响应JSON> -OfflineWorkId demo1
.\scripts\local.ps1 Run -Task offline -OfflineTaskPath <快照目录> -OfflineScriptPath <固定响应JSON> -OfflineWorkId demo1 -Resume
```

`<...>` 是要替换的参数示意，不是可直接运行的真实任务。输入路径须在仓库内且不在 `data_B/` 或 `archive/`；工作目录固定为 `.dfx-local/work/offline-<ID>/`。脚本格式是 `{"schema_version":1,"responses":[{"tool":"edit_source","args":{"path":"src/main.ets","old":"bad","new":"good"},"usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15}}],"expected":{"path":"src/main.ets","text":"value=good\n"}}`；`tool:null` 表示模型结束消息，`usage:null` 表示真实用量未知。这个 expectation 只用于合成替身，不得放入未来独立评测求解上下文。`agent-report.json` 记录候选、离线检查、调用和未知 usage；`run-state.json` 记录受管工作进程终态；SQLite 和检查意图支持恢复。恢复沿用原 ID 和预算，检查重试沿用原幂等键。被中断且尚无终态的工作目录会阻止清理，须先核对再恢复或处置。

合成输入自检位于 `tests/test_offline_evidence.py`，领域对象和候选目录反例位于 `tests/test_contracts.py`、`tests/test_workspace.py`，固定响应闭环位于 `tests/test_agent_loop.py`。这些检查不读取或运行 data_B 中的应用，不调用真实模型或设备。当前设计与待实现功能见 [技术设计](../../docs/development/system-design-draft.md) 和 [模块契约](../../docs/design-contracts.md)。
