017 OfficeAttendance1 局部源码宿主协议（evaluator only）

固定实际源码：scenes/agency/src/main/ets/mock/DataManager.ets
实际执行整个 DataManager 类，以及它自己的静态 Map、saveTodos/loadTodos、
loadTodosByState、queryTasks、queryById 和 querySubTasks；不重写查询算法。

普通 Node 必须提供官方 node:module.stripTypeScriptTypes。适配只擦除确切
TaskItem 接口 import、经核对实际未使用的 @ohos.data.preferences import，
并把 named export 绑定到局部 vm.Script。删除 import 后若仍使用preferences，
或类型擦除后仍把TaskItem当运行值，或有其他import，明确拒绝。
这不是preferences模拟器；实际原版就使用自己的Map，mocked_interfaces=[]。
vm 是装载方式，不是操作系统安全沙箱；进程继续由受管执行器限制。

四阶段 argv=[Node绝对路径,本oracle.cjs绝对路径,阶段名]：
  build       擦除类型、装载完整真实类，保留同步查询与既有方法接口。
  target      用实际saveTodos初始化固定任务，调用queryTasks('Alpha',false)。
              仅该调用产生正则构造SyntaxError时输出
              DFX017_TARGET_REGEX_SYNTAX_ERROR，退出9；超时不算复现。
  behavior    固定完成状态、大小写、中文、字面元字符、空/无匹配输入；
              每次调用都核验实际loadTodos返回的库存未被修改。
  regression  更多字面元字符/重复查询，既有按ID与父ID查询，以及库存不变。
异步初始化须完成才执行断言；运行器保留有界等待，未完成明确非零退出，
避免Node因只剩悬空Promise而提前退出0。等待失败不算目标正则异常复现。

业务依据来自实际源码的字面转义、大小写标志、严格完成状态筛选，以及
RequestProxy.queryTasks/AgencyTaskView.load对返回结果的使用。TaskItem接口
允许缺失title/isFinished；固定输入保留原来的空/缺失title不显示、未知
完成状态不混入true/false结果的行为，不借用构造答案或故障注入测试。
固定任务和预期只给evaluator，不放入solver源码、提示和读源工具结果。

scope须披露：仅该内存Map及查询方法在固定输入上的实际执行，不包含
preferences持久化、进程重启后的恢复、UI交互、设备或完整办公应用成功。
四阶段固定同一oracle哈希/independent_oracles；baseline_target_exit_codes=[9]，
baseline_fault_pattern='DFX017_TARGET_REGEX_SYNTAX_ERROR'。
source_mapping为空时必须保留任务完整过滤源码树的原相对路径；不要只截取
故障文件作为solver输入，原始data_B保持只读。

协议接纳的是DataManager公用方法恢复原有查询行为，不限定正则或字符串
实现。只在RequestProxy/UI绕开该方法的方案可能修好某条完整路径，但不在
本方法协议接受范围；不能据其失败断言整应用修法必错。该类方案须另接
实际调用端执行协议，否则只报告analysis/未验证补丁。

tests/test_office_source_replay.py仅使用合成.ets类，验证好候选、大小写丢失、
查询破坏库存、新增平台依赖及悬空异步初始化等正反例。它不是data_B运行成绩；真实原版的
目标SyntaxError仍须主流程在模型修复前受管接纳，不能冒充已实测。
