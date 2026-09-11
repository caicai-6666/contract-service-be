# 文件与文字相关性判断

> **当前状态：** 已改为文字区与全部有序文件摘要的一次整体判断，提示词为 file-text-relevance-v3；保留严格 Schema、有限顺序纠错与 true/false 的 1/0 分。不再逐文件并发或生成逐文件判定。

所属工作流见[合同沟通智能体](readme.md)，路由与计分见[业务门禁子图](business-gate.md)。模型可见规则唯一来源为 [file_text_relevance.py](../../../../app/agent/contract_communication/business_gate/prompt/file_text_relevance.py)。

---

## 用途与输入

判断当前文字是否存在明确的文件处理关联，或与本轮至少一份文件存在具体主题、对象关联。明确文件处理关联可以由文字直接指向当前或更早对话中的文件，不要求已经找到旧文件或具备完整答案。不重复判断业务范围，不采用向量相似度。

只有本轮同时存在文件摘要和非空文字才调度；只有文件或只有文字继续 skipped。文字提到旧附件并不会改变路由：若本轮没有上传文件，本节点仍不运行。后续的上下文相关性判断和历史文件恢复未因此实现。

消息构造入口为 build_file_text_relevance_messages(text, file_summaries)。system 为固定规则及真实 Schema，user 分为两个区域，以分割线分隔：

~~~yaml
# 文字区
text: 比较本次第一份与之前上传的原合同
---
# 文件摘要区
files:
  - upload_order: 1
    original_file_name: 补充协议.pdf
    display_name: 设备采购补充协议
    summary: 调整设备交付日期和付款节点
  - upload_order: 2
    original_file_name: 交付说明.pdf
    display_name: 设备交付说明
    summary: 说明包装运输和交接方式
~~~

实际输入使用“## 文字区”“## 文件摘要区”标题，各区独立安全序列化为 YAML。文件按 file_index 升序，必须是从 0 开始连续、唯一的完整序列；upload_order 为下标加一。拒绝缺项或重复，不静默截断文件列表。原始名称用于辅助文件名指代，展示名称和摘要用于理解内容；名称可能重复。UUID、路径、PDF 字节、页面、历史和私有审计不进入模型。

---

## 指代与布尔语义

- 泛指“这些附件”“它们”时结合完整文字和列表理解；明确文件操作不要求逐字匹配摘要。
- 本轮顺序限定按 upload_order 解读，不能当作全会话累计顺序；名称限定结合原始名和展示名，重复名称不能擅自确定唯一文件。
- 明确“之前上传的合同”“昨天的报告”等历史文件操作时可判 true，即便该文件不在本轮列表中。理由须说明其内容未提供，不声称已经找回、不生成条款或将旧文件映射成新附件。
- 当前与旧文件的比较同样可相关。识别文件操作意图和准确定位文件是不同任务，后者不作为这里的必备条件。
- 仅有“继续”等无法识别文件操作的文字，不借看不到的历史补造意图。被撤销的操作不计入；排除新附件、明确继续处理旧附件仍可相关。
- 用户指定当前资料进行核查，即使它不足以证明事实或存在矛盾，仍可以相关；不把无法回答当作不相关。
- 没有文件操作指代时，才依据当前摘要判断至少一份是否在主题、对象上有关联；不因小部分无关文件扣分，不凭通用词虚构不同项目或主体的关系。

这是相对于 v2 的语义调整：整体 true 不再严格表示至少一份本轮附件已经匹配，也可能仅表示用户明确引用历史文件。它不保证所有附件相关、文件可访问或整轮准入，不能用于直接选择文件或批准附件落盘。

---

## 输出与执行

节点额外通过 `file_text_relevance_feedback` 保存程序生成的业务日志，区分 true、false 和执行 failed；未调度时为空。日志不声称已读取历史文件、不直接决定整轮拒绝，由统一拒绝节点组织最终 SSE 回复，完整契约见[相关性节点的业务 hint](business-gate.md#相关性节点的业务-hint)。

schema.py 中的 FileTextRelevanceGeneration 为唯一机器契约：非空 reasoning 在前，严格布尔 result 在后。reasoning 先引用文字区依据，再解释指代或必要的摘要证据；历史引用不要求伪造文件证据。不输出逐文件数组、评分或身份映射。

node.py 提供 check_file_text_relevance_async(state, settings, max_attempts=3)，同步入口为 check_file_text_relevance。图装配使用 RunnableLambda 绑定同步与原生异步实现，输入 Schema 保留 file_summaries/text 的隔离；本节点不再接受局部 max_concurrency。其他相关性分支以及文件摘要、文件业务判断的并发不变。

一次整体判断使用一个客户端和一套消息，正常仅调用模型一次；最多三次尝试是同一完整输入的顺序纠错，不是逐文件调用。复用私有 JSON 执行器，继续受全局 MLLM 配额限制。strict=true、temperature=0、enable_thinking=false，输出上限为配置值与 1024 token 的较小值。本地拒绝非标准、重复/缺失/额外字段、空理由、非布尔结果、截断、工具调用和拒答。

错误原响应只进入私有审计，最小反馈顺序追加；全部校验通过或取消时清除整段反馈。取消向外传播并关闭客户端，不发布迟到结果。服务异常或重试耗尽返回 failed，不伪装成 false。超出模型输入容量时不静默截摘要、不分批投票，按请求失败处理；一次性输入的容量边界仍需真实多文件测试。

内部 file_text_relevance_results 已替换为单个 file_text_relevance_result，保存 completed/failed、整体布尔值，reasoning/audit 排除普通序列化。没有逐文件身份或聚合器。正式 HTTP/SSE、快照与历史字段不变。

---

## 计分与服务边界

| 情况 | 本维度结果 | 得分 |
| --- | --- | --- |
| 只有文件或只有文字 | skipped | 不参与 |
| 输入不完整、服务失败或纠错耗尽 | failed | 不形成有效总分 |
| 整体关联成立 | true | 1 |
| 整体关联不成立或依据不足 | false | 0 |

结果仍交给 aggregate_relevance 统一计分；双模态阈值保持 3，不随文件数增加。此节点与其他相关性分支仍可并行，只有本节点内部逐文件并发被取消。

正式服务继续显示“正在思考”，拒绝或失败的提示流式输出并同步 SSE、快照及历史。核心问答尚未接入，通过时仍只返回未接入提示，不批准附件落盘。历史文件指代规则不等于历史读取、文件定位或上下文节点已经接入。

---

## 依赖与验证

复用 PyYAML、Pydantic、MLLMClient，无新增依赖、配置或工具。纠错遵循[提示词工程规范](../../../standard/prompt-engineering.md)和[上下文规范](../../../standard/agent-context-management.md)。

tests/test_file_text_relevance_prompt.py 检查两个区域、完整上传顺序、原始名称、稳定前缀、结构安全及当前/历史指代示例；tests/test_file_text_relevance_node.py 检查多文件只有一个请求、顺序重排、整体计分、严格契约、纠错、取消和多任务隔离；全图与 Communication 回归覆盖拒绝、失败及附件边界。均使用模型桩，不证明 v3 的真实语义准确率。

旧 experiment/file-text-relevance 的 v2 配对实验产物保持不变；其逐文件标签和接口不能直接用于 v3 整体判断。旧运行器已添加版本保护，避免把不兼容的新运行冒充历史实验；v3 真实扩大测试尚未执行。
