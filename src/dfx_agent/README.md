# 离线证据原型

状态：当前使用说明。最后维护：2026-09-23。
职责：维护现有命令、输入格式和行为限制；项目进度见 [STATUS](../../docs/STATUS.md)。

当前命令行支持输入快照、日志解码、栈位置到源码映射及离线报告。`contracts.py` 与 `workspace.py` 另提供首批严格对象和候选 diff 登记库，但命令行尚未连接它们；尚不具备语义根因定位、模型调用或应用运行验证能力。

从项目根目录查看命令：

```powershell
python scripts/dfx.py --help
python scripts/dfx.py prepare --help
python scripts/dfx.py analyze --help
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

合成输入自检位于 `tests/test_offline_evidence.py`，领域对象和候选目录反例位于 `tests/test_contracts.py`、`tests/test_workspace.py`。这些检查不读取或运行 data_B 中的应用，不调用真实模型或设备。当前设计与待实现功能见 [技术设计](../../docs/development/system-design-draft.md) 和 [模块契约](../../docs/design-contracts.md)。
