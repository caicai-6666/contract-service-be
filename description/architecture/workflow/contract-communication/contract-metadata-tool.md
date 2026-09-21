# 合同元数据查看工具

`tool/contract_metadata.py` 集中定义参数模型、SQLite 读取处理、渲染器及注册入口。已由正式会话宿主注册到 Agent Core；在业务门禁通过后，经统一注册表、FIFO 和 Executor 执行，不创建子 Agent 或驻留池。

## 调用与返回

`get_contract_metadata(document_id)` 用于快速了解合同概况、读取摘要或核对审核人及日期。唯一参数为完整64位小写十六进制合同 ID，禁止缩写、截断、省略号或名称替代。

每次在线程中读取注入的 SQLite 元数据存储，避免阻塞异步循环。只允许 ready 合同；渲染名称、完整 ID、审核人、签署日期、入库时间和摘要。名称不追加 `.pdf`；历史缺失字段显示“未记录”，不推断或生成摘要。签署日期按存储值展示，入库时间转换为明确的 UTC+08:00。摘要仅供概览，条款核实仍需查看原文。

成功回执包含 status、document_id、content（渲染正文），类型为 ordinary，不分页、不折叠、不另存缓存。SSE 使用默认 thinking；工具结果只进入模型上下文，不直接作为用户正文发布。

## 错误边界

不存在返回 contract_not_found，正在删除返回 contract_deleting，尚未入库返回 contract_not_ready；读取异常返回 query_failed，不泄露底层异常，不将读取失败解释为合同不存在。非法 ID 在注册表参数校验阶段拒绝，不执行数据库查询。

不附带关系或注意事项，亦不读取 ES、Neo4j 或文件正文。宿主在启动期注入共享元数据存储，工具不自行获得用户身份或放宽会话鉴权。

## 验证

`tests/test_contract_metadata_tool.py` 覆盖格式、空字段、实时读取、日期时区、各错误状态及默认进度。`tests/test_communication_relation_tools.py` 验证正式宿主注册、工具调用后下一轮模型上下文获得真实渲染字段及完成任务。以上使用隔离数据和模拟模型，不调用外部模型服务。
