# 单任务记忆提取工具

## 用途与实现边界

节点2使用独立的think、extract_memory工具集合，定义与纯内存执行器位于`app/agent/conversation_memory/tool.py`，工具版本为`conversation-memory-summarizing-tools-v1`。不向单任务整理暴露select_task或finish_selection。

工具执行器只负责Schema、严格参数解析和顺序/终态校验，不访问文件、不生成向量或写数据库。节点2通过独立模型循环调用这些工具，工具完成后由程序对非空总结编码，多个任务经LangGraph Send并发处理并在节点3组装[待入库数据](pending-records.md)。提示词见[单任务整理提示词](summarizing-prompt.md)。

---

## 工具契约

| 工具 | 参数 | 用途 |
| --- | --- | --- |
| think | reasoning_summary，非空字符串，最多2000字符 | 核对原任务与提取关注点、来源、时间和适用边界，不提交正式结果 |
| extract_memory | evidence，非空字符串，最多2000字符 | 提供原任务内可定位依据；无可靠信息时说明已检查内容及缺失依据 |
| extract_memory | reasoning_summary，非空字符串，最多2000字符 | 说明整理依据与边界；正文为null时说明原因 |
| extract_memory | retrieval_text，必填，可为null或1—6000字符的非空正文 | 提交用于检索的记忆文本；null表示整个任务无可靠整理依据，不是无信息提示句 |

字段均在实际JSON Schema上提供description；工具strict=false，程序拒绝未定义字段、类型转换、空白文本、重复JSON属性和非法JSON。不从普通文本猜测工具参数。字符上限是当前接口边界，不要求模型填满。

---

## 执行顺序与结果

通过MemorySummarizingTools.execute接收单个OpenAI风格function调用列表，每个原任务创建独立实例。

- 首个成功动作必须为think，每轮恰好一个工具；不允许连续成功think。
- 正常顺序为think → extract_memory，提取成功即结束，无第三个完成工具。
- 失败调用不修改成功状态，可修正后重试当前合法动作；工具自身错误通过调用循环的tool消息反馈，协议/顺序违规抛出MemoryFlowViolation供system_guidence使用。
- result=None表示尚未提交；result为ExtractMemoryArguments表示已完成提交，即使其中retrieval_text为null。完成后冻结，不允许继续调用或改写。
- 只有retrieval_text非null才是后续可向量化文本；证据、整理理由不自动拼入该文本，task_id由调用方关联，不要求模型生成。

模型交互循环沿用正常和失败工具交互保留、成功后只清理system_guidence的约定。本执行器不持有messages；消息保留、最多128轮、连续三次失败终止及审计由node.py的共享循环管理，不与兄弟任务共享。

---

## 验证与依赖

离线测试覆盖字段说明、首轮think、合法提交、null结果、连续think、失败不推进、错误参数与JSON、不可变终态和实例隔离。程序只证明契约与顺序正确，不能证明依据充分或正文事实准确；真实模型实验见[工作流验证](readme.md#验证)。
