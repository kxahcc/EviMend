016 TouristAttraction1 局部源码宿主协议（evaluator only）

实际目标仅是日志对应模块的完整 CommonUtil 类：
  components/module_base/src/main/ets/util/CommonUtil.ets
本应用另有 commons/common/src/main/ets/utils/CommonUtil.ets 正常同名文件。
验证器强制使用上面的 module_base 路径，不能替换为正常同名文件冒充修复。

使用普通 Node 的官方 node:module.stripTypeScriptTypes 擦除完整候选类型，
只把 named export 绑定到局部 vm.Script。实际方法体完整执行，不抽取或
重写电话号码判断，不安装 npm 包，不提供平台 API mock。其他 import 或
删除 CommonUtil named export 会拒绝；缺官方类型擦除器明确拒绝。
vm 是装载机制，不是系统安全沙箱，进程继续由现有受管执行器限制。

四阶段 argv=[Node绝对路径,本oracle.cjs绝对路径,阶段名]：
  build       类型擦除并装载实际类，核对三个既有方法仍存在。
  target      实际调用 verifyPhoneNo('13800138000')；原版在构造正则时的
              SyntaxError 输出 DFX016_TARGET_REGEX_SYNTAX_ERROR，退出9。
              超时、类型擦除失败、错误路径和其他异常不算目标故障复现。
  behavior    固定合法/非法前缀、空串、长度、非数字、空格、国际前缀和
              全角字符输入，拒绝只捕获异常后恒定返回或丢失原有校验规则。
  regression  padWithZeros 的正常边界、verifyCardNo 的既有格式判据，
              以及交错重复电话号码校验。

独立业务依据是应用现有正常同名文件的电话号码前缀/长度规则，以及
module_base 的真实导出和 TouristVM.validate 对该方法的调用。
这只保持该应用已有规则，不宣称是现行全部电话号码或身份证合法性标准；
固定测试不是第二份运行中的判断算法。资源、预期和运行器不进入 solver
源码、提示或读源工具结果；实际失败可作为既有受控反馈返回。

配置 LocalCheckConfig(mode='source')，mocked_interfaces=[]，scope 填写
上述局部方法、类型擦除、固定覆盖及无UI/提交覆盖。四阶段固定同一 oracle
哈希和 independent_oracles，baseline_target_exit_codes=[9]，
baseline_fault_pattern='DFX016_TARGET_REGEX_SYNTAX_ERROR'。
空 source_mapping 仅表示任务完整过滤源码树与宿主源码树相对路径一致；
准备任务不能只保留这个已知文件，原始 data_B 不得修改。

验收范围与替代修法：
本协议接纳使实际 module_base 公用方法恢复既有校验行为的实现，不限制
具体正则写法。仅在调用端捕获异常、绕开此方法、改用另一个 CommonUtil
的方案，可能修好某条完整应用路径，但不会通过本公用方法协议；不能因此
判定这些方案在整应用上一定错误。若接受该类替代修法，须另接包含真实
调用端路径的独立执行协议，否则仅保留 analysis/未验证补丁。

tests/test_tourist_source_replay.py 用合成 .ets 类验证正确候选、恒定接受、
破坏无关方法、只修改正常同名文件等正反例。它不是 data_B 成绩。
真实原版的装载及目标异常须由主流程先受管接纳，不能用合成通过替代。
