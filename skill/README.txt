EviMend Skill 库
===============

skill/ 是本项目维护 Skill 的唯一源目录。这里保存给修复 Agent
使用的领域过程及其配套材料；不需要安装到 Codex 的个人 Skill 目录。

当前四份自设计 Skill：

- bounds/SKILL.md：范围条件、容量与单位、完整数据和尾部处理。
- lifecycle/SKILL.md：初始化、异步依赖、对象归属、取消和资源释放。
- recursion/SKILL.md：递归进展、环与深链、访问策略和完整遍历结果。
- dynamic-pattern/SKILL.md：字面查询、用户正则、结构化解析和结果语义。

每份正文都包含适用线索、前提与未知问题、步骤与分支、必须保留的行为、
反例和退出条件。它们指导调查和修法选择，不包含评测任务答案；阅读 Skill
不能替代日志与代码查证、实际应用检查或修复接受判定。

标准目录和文件格式
------------------

新增或引入外部 Skill 时，使用独立子目录，名称采用小写字母、数字与短横线。
SKILL.md 顶部用 YAML frontmatter 提供 name 和 description；name 与目录名称
一致。其后写普通 Markdown 工作过程，无需再写对应的 JSON 定义。

示意结构：

skill/
  my-skill/
    SKILL.md
    references/    可选，按需阅读的知识与接口说明
    scripts/       可选，需显式允许执行的辅助脚本
    assets/        可选，模板或其他静态材料

外部材料也放入所属 Skill 子目录，不维护另一套生效的 Skill 源目录。
第三方 Skill 应保留原许可证与来源，并在接入前核对目标平台、工具要求和
业务适用性；有 Skill 文件不代表其中脚本已经获得执行权限。

加载约定
--------

按需加载采用渐进披露：先给 Agent 名称与 description 目录，选中适用 Skill
后读取完整 SKILL.md，再按当前问题读取 references 等配套材料。不在每轮
请求中无条件展开整个库，也不把引用文件的存在当成已读取其内容。

强加载使用标准 frontmatter 的 metadata 扩展，给少量必要包显式设置：

metadata:
  dfx-load: always

该包的完整 SKILL.md 会随启用的目录进入每次模型请求，计入上下文与预算；
配套文件仍按需读取。省略 dfx-load 或设置 on-demand 表示按需加载。
这是本系统的宿主约定，不是 Agent Skills 标准自带的通用强加载字段。
当前四份领域过程默认按问题选择，不自动强加载、替换系统规则或执行脚本。
static_skills_enabled=false 关闭目录和强加载注入，保留共同原件及读取工具。

读取示例：read_skill(skill_id="bounds") 获取整个正文；读取其包内实际存在的
资料时设置 resource_path="references/guide.md"。引用路径相对该 Skill 包，
不能跳到其他目录。当前接入只支持 UTF-8 文本；含二进制文件的包会明确拒绝。
普通 search_experience 仍返回完整指导投影，可能包含整个包的配套文本；
需要逐份控制上下文时优先使用 read_skill 的正文及 resource_path 读取。
验收结果与尚未验证的效果统一见 docs/STATUS.md。

运行时为复现任务保存的冻结副本与记录属于 .dfx-local 下的运行产物；
它们不作为新增或修改 Skill 的入口。维护正文只修改本目录，历史任务继续
使用当时保存的内容，不因本目录更新而静默改变。
