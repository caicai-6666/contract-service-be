# 单任务检索记忆整理提示词

## 用途与状态

节点2提示词位于`app/agent/conversation_memory/prompt/summarizing.py`，版本为`conversation-memory-summarizing-v3`。目标是按单任务的提取要求生成可检索记忆正文，不重做筛选、不生成整个会话的上下文压缩摘要。v2移除单任务编号；v3改用think和extract_memory工具提交，末尾拼接启动加载的tool_tag。

提示词及[两工具契约与执行器](summarizing-tools.md)已接入process_memory_tasks的独立模型循环，通过Send并发运行；程序随后对非空正文向量化，由节点3汇总[待入库数据](pending-records.md)。本节点不执行落盘，由已接入的[归档Service](../../system/communication-archive.md)负责。原先的直接文本输出与无依据固定提示语已撤销。

---

## 输入与消息结构

`build_memory_summarizing_messages(task: MemoryTaskInput, selection: SelectedMemoryTask)`返回system、user两条消息。重新校验参数，并检查两者task_id一致，不一致立即报错，避免把其他任务的提取要求传入。

- system：固定任务描述、事实依据、六种状态语义、整理规则、输出和失败边界及示例，随后为工具顺序、纠错约定，最后附tool_tag。
- user：只呈现当前任务的原始创建时间、状态、用户输入、附件名称、有序有效轨迹，以及selection.extraction_requirements。
- 不传其他任务、筛选理由或筛选证据，避免它们替代原始事实；真实task_id及批内序号不作为模型事实输入。

复用节点1公开轨迹投影：显示+08:00原任务时间，缺失不补填；过滤interrupted助手消息，保留工具状态及原轨迹位置；附件只显示名称，不读文件。节点2不展示任务序号字段或“任务1”标题，仅保留“当前任务”标题及内部轨迹序号。节点1仍默认展示批内任务编号，原始业务文本不做编号字符串替换。构造器不访问外部资源，也不修改原始任务。

---

## 核心设计

- 原始任务优先于提取要求：指导中的无依据数值和结论不得进入记忆，原始任务的重要限定即使指导遗漏也须保留。
- 单任务与部分历史：看不到前文时只整理本轮明确的变化，不补写旧方案，不为补全背景读取其他任务。
- 来源与状态：区分用户要求、用户陈述、工具观察、外部参考、计划及已执行操作，不将估算改成批准，不将失败核验改成否定结论。
- 时间与限制：忠于原文数值和单位，已有相对日期按有效锚点解释，计算结论连同适用边界保留。
- 压缩但不丢边界：删除重复话语和阶段播报，不删重要的未知口径、前置依赖或授权条件。

通过extract_memory提交简洁中文正文，不在工具调用之外直接输出正文；retrieval_text自身不添加引导语、代码块或JSON外壳。工具详细描述由请求中的tools提供，不复制到user任务列表。模型循环使用before_task和tool_task_index=1，将工具固定在初始任务前，不随后续交互移动。

个别指导无依据时省略该部分并整理其余有效内容；整个任务无可靠信息时，通过extract_memory提交retrieval_text=null，并在依据和整理理由中说明缺失情况。节点2对null确定性跳过向量化；模型是否确实缺少有效依据仍需语义核对，不能仅以Schema通过证明判断正确。

---

## 使用与验证

从`app.agent.conversation_memory.prompt`导入构造器、MEMORY_SUMMARIZING_SYSTEM_PROMPT和版本常量。构造器使用启动加载的tool_tag，独立脚本须先初始化。图已依据task_id匹配任务、隔离并发并校验输出；没有调用Embedding。

离线测试覆盖任务匹配、稳定system、确定性渲染、缺失时间、敏感和中断内容过滤、输入不变及提示词边界。真实模型实验见[工作流验证](readme.md#验证)；示例取自已审阅场景，不能将相同场景的后续结果视为独立泛化测试。

相关入口：[工作流](readme.md)、[筛选提示词](planning-prompt.md)、[提示词规范](../../../standard/prompt-engineering.md)。
