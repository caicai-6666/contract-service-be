# 文件业务相关性判断

> **当前状态：** 已接入逐文件并发判断、严格 JSON Schema 约束解码、有限纠错与文件维度聚合。仅依据名称和摘要，不重复读取视觉页面。

所属工作流见[合同沟通智能体](readme.md)，调度与现有聚合边界见[业务门禁子图](business-gate.md)。模型可见规则的唯一来源为 `app/agent/contract_communication/business_gate/prompt/file_business_relevance.py`，版本 `file-business-relevance-v2`。

---

## 目标与输入

依据单份文件的生成展示名 display_name 和内容摘要 summary 判断文件本身是否属于可处理的业务材料。文件范围包括财务、法律、合同、经营报表、采购履约资料、业务设备与工程技术资料、经营管理材料；技术资料不要求已关联具体交易。

`build_file_business_relevance_messages(*, display_name, summary)` 仅接受两个非空字符串，返回固定 system 规则和 user YAML 两条消息。YAML 按 display_name、summary 顺序渲染，保留原字符串值；不接受原始文件名、ID、上传文件对象、页面图像、用户文字、会话历史或其他文件。

程序从 FileSummary 显式提取这两项，以 file_index 和原始文件名绑定结果；模型不生成身份。单份文件一个独立会话，多文件有界并发，结果按原始下标排序，同名文件不会混淆。

---

## 判定与输出约定

- summary 是内容判断的主要依据，display_name 辅助识别；依据实质内容与用途，不仅看名称包含“合同”“报表”“图纸”就放行。
- 摘要明确披露独立的非业务实质内容时，即便文件其他部分相关，也将该文件判 unrelated；业务部分、篇幅占比或附页位置不能抵消。
- 作为业务背景、引用、证据或必要附件的内容不自动算无关。用途不明且不足以判断是否独立时为 uncertain，不自行补造业务联系。
- 本次不增加全文自洽性检查、恶意标签或新熔断分支；摘要未披露的内容不能检出。合同条款矛盾本身不代表业务无关。
- 名称与具体摘要冲突时保留冲突说明，以摘要实际内容为主；资料笼统或冲突导致主题无法确认时使用 uncertain。
- 不以通用表格、数字、线条推断业务主题，不假设采购关系或上传意图；不判断与当前用户问题的匹配度。
- 技术资料属于本文件维度的业务材料范围，不因此自动修改文字维度对通用技术咨询的定义。

输出字段顺序为 reasoning、result。reasoning 先引用名称或摘要的短原文，再说明依据，不限制语言、不虚构原文件页码。result 为 related（明确相关）、uncertain（不足以判断）、unrelated（明确无关）。执行故障不是 uncertain。

`schema.py` 的 `FileBusinessRelevanceGeneration` 是唯一机器契约。客户端发送 strict JSON Schema，开启原生 thinking、使用 temperature=0；本地再次拒绝缺失、额外或重复字段、布尔值、空理由、非标准 JSON、代码块、截断、拒答和工具调用。思考与最终 JSON 共用全局 max_completion_tokens，不再另设 1024 上限。

---

## 并发、纠错与聚合

`node.py` 提供 `check_file_business_relevance_async(state, settings, max_concurrency=4, max_attempts=3)`，输入仅为 file_summaries；同步 invoke 使用适配入口。业务门禁构造器传递相同的局部并发、重试配置，实际请求同时受全局 MLLM 配额约束。

- 使用有界工作协程，不按文件总数创建等待任务；每份文件独立客户端、消息、重试计数与审计。
- 默认最多三次尝试，可配置 1～3 次。校验错误只追加具体字段问题与修正方向，不回显完整旧输出。全部校验通过后删除整段反馈；原始响应与反馈仍保留私有审计。服务异常直接失败，不伪装业务判断。
- 取消向外传播，并等待所有工作协程回收，不继续启动队列中的文件或发布迟到结果。
- 输出 `file_business_relevance_results` 保留全部逐文件 status/result 与程序绑定身份；reasoning 和 audit 仅内部保留，排除普通序列化。结果不新增为 HTTP/SSE 或持久化字段。
- 所有文件执行结束后统一汇总；执行失败优先，不能被另一份文件的 related 掩盖。

| 整批情况 | 文件维度结果 | 得分 |
| --- | --- | --- |
| 无文件 | skipped | 不参与 |
| 任一文件执行失败 | failed | 不形成有效总分 |
| 全部成功且至少一份 related | related | 2 |
| 无 related，但至少一份 uncertain | uncertain | 1 |
| 全部 unrelated | unrelated | 0 |

不按相关文件比例降分，也不随文件数量累加：一份业务设备资料混入多份无关资料，文件维度仍得 2 分。此规则只合成文件维度，不单独决定整轮准入；其他适用分支必须完整执行，最终阈值由[业务门禁聚合](business-gate.md#加权与阈值聚合)统一处理。

聚合出口通过 `file_business_relevance_feedback` 保存一份 `RelevanceFeedback`（`node/result/hint`），无文件时为 `None`。只在节点层生成 hint，不向逐文件结果添加 feedback 或 hint。全部检查完成且至少一份相关即给出正向提示，不逐一列举无关文件；存在执行失败仍优先生成 failed 反馈。非法摘要输入也生成整体失败反馈，不猜测具体问题文件。文案与消费方式统一见[相关性节点的业务 hint](business-gate.md#相关性节点的业务-hint)，不改变本维度的模型请求、私有审计或计分；最终 SSE 正文由统一拒绝节点生成。

没有历史时，无论只有文件还是同时含文字，现已可得到 passed/rejected；含有效历史会调度已实现的上下文维度。完整门禁通过后，正式服务按[当前执行边界](business-gate.md#检查后的流转)继续，文件维度单独相关不等于整轮通过或已完成业务分析。

---

## 依赖与验证

复用 PyYAML、Pydantic、MLLMClient 和全局模型配置，无新依赖、环境变量、HTTP/SSE 字段或持久化字段。执行与纠错遵循[提示词工程规范](../../../standard/prompt-engineering.md)及[上下文管理规范](../../../standard/agent-context-management.md)。本节点不使用函数工具，采用 Schema 校验的等价清理流程。

`tests/test_file_business_relevance_prompt.py` 和 `tests/test_file_business_relevance_node.py` 覆盖提示词、严格 Schema、输入隔离、纠错清理、故障、并发上限、乱序结果、取消与聚合规则。聚合及 Communication 回归覆盖状态、流式提示、历史备份和附件边界。测试使用模型桩，不代表真实模型语义准确率已验证。


## v2 验证边界

本版本仅收紧单文件实质内容与用途的判断，输出仍为 reasoning/result 三态。多文件聚合继续“至少一份 related 则文件维度 related”，不将本次单文件规则等同于任一文件拒绝整轮。历史实验按v1标注的混合内容用例不能直接用原标签评价v2；原始产物保持不变。

定向验证包括7份合成摘要（全部符合预期）及真实五页合同插入单页艺术内容的配对测试：原件通过，混合件摘要披露第3页无关艺术内容并被拒绝。拒绝自然语言仍有未具体指出问题页的不足。证据和限制见[真实单页插入分析](../../../../experiment/file-mixed-page/output/20260915T093834.719415Z/analysis.md)，不代表全文插页检出率保证。
