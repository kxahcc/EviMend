EviMend 软件测试
================

本目录包含模型协议、SDK 工具循环、候选与来源、上下文、Skill、宿主执行、
恢复、预算和运行管理的回归测试。它们证明对应软件行为，不自动证明研究收益。

从项目根使用原生受管入口运行目标文件：
.dfx-local\env\Scripts\python.exe -B -m dfx_agent.managed_runtime --workspace . Run -Task test -TestPath tests/test_product_cli.py -TimeoutSeconds 180

多数测试使用合成任务或模拟服务。源码回放、历史结果包等测试还需要各自声明
的本机输入与运行器；先确认条件，不凭文件名推定可以无数据执行全量测试。

真实模型、多项目修复、独立判据和方法对照按 docs/preparation/metrics-and-gates.md
另外接纳。当前研究进度和实际测试结果统一在 docs/STATUS.md 维护。
