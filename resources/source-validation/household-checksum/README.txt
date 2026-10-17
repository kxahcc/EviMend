022 HouseholdService1 局部源码宿主验证器（evaluator only）

目的：用普通 Node 装载实际原版/候选 OrderRequestChecksum.ets 的完整类，
执行 create/verify。它不是通用 ArkTS 编译器，不代表整应用、订单插入、
PRE_ORDER 路由、页面、网络服务器或 HarmonyOS 平台已经验证。

运行时条件：普通 Node 必须提供 node:module.stripTypeScriptTypes。
不支持时明确拒绝，不降级为正则类型解析器，不安装 npm 包或 SDK。
适配仅擦除确切的 IRequest 接口 import、由 Node 擦除类型、将命名 export
绑定到局部 vm.Script。类体及候选计算逻辑不替换；其他 import 明确拒绝。
vm 只用于装载，不是操作系统安全沙箱；进程由现有受管 Job 和超时控制。

验证目标相对项目根的路径：
  commons/lib_foundation/src/main/ets/http/OrderRequestChecksum.ets

四阶段 argv：
  [普通Node绝对路径, 本oracle.cjs绝对路径, 阶段名]
允许显式第四项给上述相对目标路径；默认即该路径。

  build       类型擦除、实际局部装载、create/verify/header 接口检查。
  target      ASCII固定请求调用实际 create；原版 RangeError 输出
              DFX022_TARGET_RANGE_ERROR，退出9；候选必须匹配固定校验值。
  behavior    ASCII/中文/emoji固定checksum；实际verify接受原请求，拒绝错误
              checksum和改变actualPay的请求；检查原输入不被修改。
  regression  冻结请求、跨输入重复调用、空/前导零/大小写checksum拒绝。

固定请求和预期来自 resources/preparation/case-oracles.json（SHA256
ed48a6f30641f9587dc28de49c3e5377cf08bc24bc1c68727c113a0b1699c95b）。
这里只冻结 create/verify 必要预期，不沿用该资源中尚未实现的路由/订单断言。
这些内容不得进入 solver 的任务源码、工具读源结果或提示上下文。
每阶段输出读取到的原版/候选 SHA256，支持执行器核对实际执行身份。

LocalCheckConfig 使用 mode=source，mocked_interfaces=[]（无平台API mock），
validation_scope 明确填写本文件的局部执行范围和类型擦除限制。
project_root 是宿主认可的原版源码项目；空 source_mapping 表示任务快照与
项目源码相对路径一致。若使用显式映射，必须完整覆盖该任务快照，不只映射
故障文件。四阶段的 oracle_sha256/independent_oracles 固定本文件的哈希；
baseline_target_exit_codes=[9]，baseline_fault_pattern=DFX022_TARGET_RANGE_ERROR。
适配、固定判据和原版复现须在模型生成候选之前接纳。

tests/test_household_source_replay.py 使用合成 .ets 类及实际受管 Node 执行，
验证好补丁通过、取巧补丁失败、未知import和缺失类型擦除器明确拒绝。
合成测试不是 data_B 运行成绩；原始 data_B 保持只读，真实模型与案例测试
由主流程在预算和数据调用边界获准后执行。
