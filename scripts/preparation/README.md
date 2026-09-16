# 数据准备工具

状态：当前工具说明。最后维护：2026-09-16。

目前受维护的准备工具只有 `inspect_dfx_data.py`。从仓库根目录运行 `python scripts/preparation/inspect_dfx_data.py`，只读检查本地 data_B 的应用、日志、ZIP 内容和源码映射，输出 resources/preparation/data-b-inventory.json。它不联网，不执行数据中的脚本，不修改应用源码；结果不是根因标签或修复成绩。

重新生成清单时同步核对 [数据报告](../../docs/preparation/data-b-assessment-2026-09-15.md) 的统计和证据日期。当前运行准备与阶段条件见 [实施计划](../../docs/project-work-plan.md)。
