# 合同元数据查看工具

`tool/contract_metadata.py` 集中定义参数模型、SQLite 读取处理、渲染器及注册入口。已由正式会话宿主注册到 Agent Core；在业务门禁通过后，经统一注册表、FIFO 和 Executor 执行，不创建子 Agent 或驻留池。

## 调用与返回

`get_contract_metadata(document_id)` 用于快速了解合同概况、读取摘要或核对上传人及日期。唯一参数为完整64位小写十六进制合同 ID，禁止缩写、截断、省略号或名称替代。

每次在线程中读取注入的 SQLite 元数据存储，避免阻塞异步循环。只允许 ready 合同；渲染名称、完整 ID、上传人、签署日期、入库时间和摘要。名称不追加 `.pdf`；历史缺失字段显示“未记录”，不推断或生成摘要。签署日期按存储值展示，入库时间转换为明确的 UTC+08:00。摘要仅供概览，条款核实仍需查看原文。

成功回执包含 status、document_id、content（渲染正文），类型为 ordinary，不分页、不折叠、不另存缓存。SSE 使用默认 thinking；工具结果只进入模型上下文，不直接作为用户正文发布。

---

## 按通行证获取合同

`get_contract_by_passport(passport, page=None)` 查询一张通行证关联的多份合同，并在同一工具内翻页。工具、参数模型、缓存池和渲染入口独立定义于 `tool/contract_passport.py`，仅基本信息排版复用 `render_contract_metadata`。正式宿主注册后，经 FIFO 与 Executor 执行，SSE 沿用 `thinking`。

- `passport`：完整、非空、无首尾空白，保留大小写，不限定为 UUID；不得使用合同 ID 或消息 ID 替代。
- `page`：列表页码，从1开始。省略或 null 时首次第1页、后续下一页；显式传1重新查询并刷新快照，其他页码跳页。末页返回 `end_of_results`，非法页返回 `invalid_page`，均不推进游标。

通行证不是合同唯一键。一张通行证可以关联多份合同；SQLite 参数化等值查询全部 ready 合同 ID，按入库时间倒序、同时间按 ID 升序排列。历史未记录 passport、未完成入库或删除中的合同不进入新快照。不读取 PDF 页面，不调用外部平台。

每会话一个独立 LRU 池，以通行证为键，只缓存合同 ID 快照、资源标识和分页位置，不缓存渲染正文。默认驻留10个查询，每页5份合同，可分别通过 `COMMUNICATION_CONTRACT_PASSPORT_CACHE_MAX_QUERIES` 和 `COMMUNICATION_CONTRACT_PASSPORT_PAGE_SIZE` 配置。会话释放时关闭池；被驱逐后再次调用会重查，省略页码从第1页开始。不同会话的游标及展示引用互不通用。

非空结果返回 `foldable` 文本页面引用，回执包含 passport、page、total_pages、count。页面渲染通行证、当前页/总页数、合同总数，以及每份合同的名称、完整 ID、摘要、上传人和日期；正文按现有展示窗口折叠。展示时实时读取元数据；已删除或不可用条目保留位置并提示刷新，不把后续合同补进旧快照。旧引用在刷新或驱逐后失效，带签名的页面引用不可伪造。

空结果为成功的普通反馈，count=0、total_pages=0，不创建缓存或结果 ID。查询故障返回 `query_failed`，不伪装成空结果；会话释放返回 `session_unavailable`。无需另行注册翻页工具。

`tests/test_contract_passport_tool.py` 验证一通行证多合同、分页、刷新、驱逐重建、空结果、非法页、引用隔离、实时删除、旧库索引迁移和正式主循环接入。使用隔离数据库与模拟模型，不代表真实模型调用质量测试。

---

## 错误边界

不存在返回 contract_not_found，正在删除返回 contract_deleting，尚未入库返回 contract_not_ready；读取异常返回 query_failed，不泄露底层异常，不将读取失败解释为合同不存在。非法 ID 在注册表参数校验阶段拒绝，不执行数据库查询。

不附带关系或注意事项，亦不读取 ES、Neo4j 或文件正文。宿主在启动期注入共享元数据存储，工具不自行获得用户身份或放宽会话鉴权。

## 验证

`tests/test_contract_metadata_tool.py` 覆盖格式、空字段、实时读取、日期时区、各错误状态及默认进度。`tests/test_communication_relation_tools.py` 验证正式宿主注册、工具调用后下一轮模型上下文获得真实渲染字段及完成任务。以上使用隔离数据和模拟模型，不调用外部模型服务。
