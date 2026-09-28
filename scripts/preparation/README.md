# 数据准备工具

状态：当前工具说明。最后维护：2026-09-28。

目前受维护的准备工具只有 `inspect_dfx_data.py`。它只读检查本地 data_B 的应用、日志、ZIP 内容和源码映射，输出 resources/preparation/data-b-inventory.json；不联网，不执行数据中的脚本，不修改应用源码，结果不是根因标签或修复成绩。现有全量清单采集于 2026-09-15；重新盘点前须先为该脚本接入受管 `Run` 任务，目前不使用直接 Python 命令绕过本机资源入口。

重新生成清单时同步核对 [数据报告](../../docs/preparation/data-b-assessment.md) 的统计和证据日期。当前运行准备与阶段条件见 [实施计划](../../docs/project-work-plan.md)。
