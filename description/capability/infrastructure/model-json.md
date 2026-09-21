# 模型结构化输出兼容

> **用途：** 统一处理模型服务将工具嵌套对象或数组编码成 JSON 字符串的情况，避免同一输出在不同 Agent 中得到不同解析结果。

---

## 1. 接入边界

共享实现位于 `app/infrastructure/model_json.py`。转换放在模型输出的类型校验入口，顺序为：完整 JSON 解析 → 依据目标 Schema 解码内嵌容器 → 原有类型及业务校验 → 候选提交。原始模型响应、工具 arguments、审计和 function calling 轨迹保持原样；兼容后的对象只用于本地校验和执行。

目前接入合同识别、PDF 候选查重、文档结构与视觉定位、分类与未映射描述、字段与条款提取、文件命名、检索关注点与问题生成、会话记忆工具、业务门禁强制 JSON、主助手工作区与交互工具、工作区压缩子 Agent、FIFO 主题规划及主题摘要生成。

普通文本生成没有结构转换需求；历史渲染、数据库反序列化、请求 DTO、内部已验证状态不使用此兼容层。嵌入向量也不属于本模块。

---

## 2. 接口与规则

| 接口 | 职责 |
| --- | --- |
| `load_model_json(raw)` | 读取完整 JSON，拒绝重复键、NaN、Infinity 和浮点溢出；不截取代码块、不补括号。 |
| `normalize_model_json(value, schema)` | 根据对象属性、数组元素、本地 `$ref`、可空及联合类型递归转换；返回新容器。 |
| `validate_model_payload(model, payload)` | 工具参数入口；兼容后执行原 `model_validate`。 |
| `validate_model_json(model, raw, **kwargs)` | 强制 JSON 输出入口；兼容后继续使用 `model_validate_json`，保留严格 JSON 模式对 tuple/date 等类型的语义及 context。 |

字符串字段及未指定类型的自由字段保持原样。例如正文为 `{"a":1}`，不会被改成对象。数字、布尔值不会由字符串自动转换；未知字段、缺失字段、枚举、引用关系和状态机约束仍由原校验器拒绝。内嵌 JSON 的每层均使用相同严格读取规则，递归上限为 64。

联合类型允许字符串时优先保留字符串；多个容器分支只有转换结果一致时才接受通用转换，无法确定时交给原校验器处理，不擅自选择分支。新增具有业务判别字段的工具，应在解析入口根据判别字段明确目标 Schema。

工作区的 `value` 同时允许文本、对象、列表，因此由 `path` 消歧：新增信息、规划、探索结果及替换完整条目解码对象；替换 `information_ids` 解码数组；任务文本、补充、content、source 等文本位置保留原字符串。随后仍校验完整工作区，悬空引用或非法路径不能因转换而被接受。

---

## 3. 使用与验证

工具解析器使用：

```python
payload = load_model_json(raw_arguments)
arguments = validate_model_payload(Arguments, payload)
```

强制 JSON 解析器使用 `validate_model_json(Output, response.content)`，业务节点仍负责拒答、截断、工具协议与纠错处理。自定义校验异常需要把兼容失败转换为已有错误反馈类型。不要在 MLLM 客户端直接改写响应，也不要将兼容转换当成工具执行成功。

无需新增配置，依赖 Python 标准 JSON 与 Pydantic。针对性验证见 `tests/test_model_json.py`：递归容器、文本保真、工作区路径与引用、数值及重复键拒绝、门禁错误契约、真实摘要消费入口和原始审计保持。真实模型运行与历史参数回放见 [Agent Core 兼容复测](../../../experiment/agent-core-live/output/20260915T052913.534321Z/analysis.md)。
