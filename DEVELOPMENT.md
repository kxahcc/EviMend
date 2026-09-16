# 开发导览

状态：当前开发入口。最后维护：2026-09-16。
职责：说明仓库结构、文档归属与工具入口。最新进度只看 [STATUS](docs/STATUS.md)，本文不重复维护完成度和下一步。

## 如何阅读

先读 [当前状态](docs/STATUS.md)，再读 [实施计划](docs/project-work-plan.md)。具体方法在 [开发设计](docs/development/system-design-draft.md)，对象和规则在 [模块规格](docs/design-contracts.md)，如何衡量效果在 [评测口径](docs/preparation/metrics-and-gates.md)。正式提交 README 与技术/测试文档保持模板用途。

## 文档登记表

表中的“当前”表示唯一在用的文档，不代表其设计已经实现。证据文档的采集日期反映观察时间；提交模板的填写日期不承担开发版本管理。所有受维护 Markdown 均在此登记。

<!-- document-registry:start -->
| 路径 | 类型 | 唯一职责 |
|---|---|---|
| `DEVELOPMENT.md` | 当前入口 | 阅读顺序、结构、文件职责与文档登记 |
| `AGENTS.md` | 当前规则 | 助手工作流程与维护约定 |
| `docs/STATUS.md` | 当前状态 | 进度、未决条件、下一步、实测状态 |
| `docs/project-work-plan.md` | 当前计划 | 阶段顺序、依赖、完成门槛、设计缺口 |
| `docs/development/system-design-draft.md` | 当前设计 | 架构、算法与后端抽象 |
| `docs/design-contracts.md` | 当前规格 | 字段不变量、状态转换、规则卡 |
| `docs/preparation/metrics-and-gates.md` | 当前评测 | 指标、对照与验收口径 |
| `docs/preparation/data-b-assessment-2026-09-15.md` | 当前证据 | 数据事实、异常及来源；文件日期是采集日期 |
| `docs/preparation/prior-art.md` | 当前证据 | 文献查阅事实、可借鉴内容及限制 |
| `src/dfx_agent/README.md` | 当前说明 | 离线原型命令、输入格式和边界 |
| `scripts/preparation/README.md` | 当前说明 | 数据盘点工具用法 |
| `README.md` | 提交模板 | 竞赛总入口 |
| `docs/technical-design.md` | 提交模板 | 正式技术设计 |
| `docs/test-report.md` | 提交模板 | 正式测试报告 |
<!-- document-registry:end -->

## 目录与文件

| 位置 | 用途 |
|---|---|
| `docs/` | 上表中的阶段、设计、规格、评测与证据文档 |
| `src/dfx_agent/` | Python 离线证据整理原型；模块职责见下表 |
| `scripts/dfx.py` | 加载 src 包，提供 doctor / prepare / analyze 入口 |
| `scripts/check_docs.py` | 文档登记、链接、维护元数据及 archive 跟踪检查 |
| `scripts/preparation/inspect_dfx_data.py` | 只读盘点本地 data_B；不执行数据包脚本 |
| `tests/test_offline_evidence.py` | 合成输入自检；最近测试结果由 STATUS 记录 |
| `resources/preparation/data-b-inventory.json` | 数据盘点结果；不是根因标签或修复成绩 |
| `resources/preparation/sources.json` | 研究来源登记；不是当前实施配置 |
| `sessions/` | 按工作批次保存的历史摘要，允许保留当时的旧路径与决定 |
| `skill/` | 项目 Skill 的模板位置；实际实现状态见 STATUS |
| `data_B/` | 本地竞赛数据；Git 忽略 |
| `runs/` | 临时脚本、自检和运行产物；Git 忽略 |
| `archive/` | 失效文档及配套材料；Git 忽略，不作为当前入口或运行依赖 |
| `pyproject.toml` | Python ≥ 3.11 的元数据和 pytest 路径配置，非完整部署清单 |
| `.gitignore` | 排除本地数据、归档、运行产物、缓存、虚拟环境和凭据文件 |
| `.gitkeep` / 各目录 README.txt / template-session.json | 竞赛模板的占位及格式说明 |

## src 模块职责

当前命令行为以 [原型说明](src/dfx_agent/README.md) 和代码为准，项目整体能力以 STATUS 为准。

| 文件 | 职责与边界 |
|---|---|
| `task.py` | 指定日志/ZIP 成员、文件筛选、分析快照和哈希校验；不生成完整运行工程 |
| `logs.py` | 编码、错误字段与栈帧解析；不推理业务根因 |
| `sources.py` | 源码路径匹配、歧义和邻近片段；不是 ArkTS 数据流分析器 |
| `analysis.py` | 汇总栈证据；源码命中不能当作根因预测 |
| `runtime.py` | 当前仅观察 PATH/模块/环境变量存在性；不启动 SDK/设备 |
| `cli.py` | 参数与命令分发；不包含 repair 命令 |
| `__init__.py` | Python 包入口 |

## 查看与检查

从仓库根目录运行：

```powershell
python scripts/dfx.py --help
python scripts/check_docs.py
```

已安装 pytest 时，可运行 `python -m pytest tests/test_offline_evidence.py -q`。它只使用合成输入，不启动模型或应用。盘点工具用法见其 README，真实任务输入必须显式提供经核验的清单。

## Git 与归档

本地 Git 保存当前源码、模板、文档和历史工作摘要，尚未配置远程。`git status --short` 查看变更，`git log --oneline -5` 查看批次。当前版本只保留一套生效决策，旧版本通过 Git 查询。

失效材料移入 archive 并从 Git 索引移除；目录中的本地说明记录替代入口。新克隆不会含本地归档，当前文档/脚本不得依赖它。历史 session 是已发生行为的记录，旧日期和旧路径不视为当前决策；不为消除历史差异而篡改它们。

新增当前 Markdown 先确认独立职责，再登记；其余细则统一见 [AGENTS](AGENTS.md)。
