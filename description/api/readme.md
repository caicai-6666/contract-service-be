# API 参考

> **用途：** 本页是外部 HTTP 与流式接口的统一入口，定义全局路径、媒体类型、错误格式和资源文档导航。

## 接口目录

按业务文档分组，点击接口名称可直接跳转到说明章节。

| 接口 | 方法 | 完整路径 |
| --- | --- | --- |
| [健康检查](#健康检查) | `GET` | `/contract/api/health` |
| [使用密钥登录](auth.md#使用密钥登录) | `POST` | `/contract/api/auth/login` |
| [获取所有已入库合同元数据](contract.md#获取所有已入库合同元数据) | `GET` | `/contract/api/contract/documents` |
| [删除正式合同](contract.md#删除正式合同) | `DELETE` | `/contract/api/contract/documents/{document_id}` |
| [获取合同类别列表](contract.md#获取合同类别列表) | `GET` | `/contract/api/contract/categories` |
| [获取 Core 审核表单定义](contract.md#获取-core-审核表单定义) | `GET` | `/contract/api/contract/core-definitions` |
| [列出尚未入库的运行](contract.md#列出尚未入库的运行) | `GET` | `/contract/api/contract/extraction-runs` |
| [上传 PDF 并创建任务](contract.md#上传-pdf-并创建任务) | `POST` | `/contract/api/contract/extraction-runs` |
| [获取当前状态与提取结果](contract.md#获取当前状态与提取结果) | `GET` | `/contract/api/contract/extraction-runs/{run_id}` |
| [取消任务](contract.md#取消任务) | `DELETE` | `/contract/api/contract/extraction-runs/{run_id}` |
| [确认查重并继续](contract.md#确认查重并继续) | `POST` | `/contract/api/contract/extraction-runs/{run_id}/continue` |
| [订阅处理事件](contract.md#订阅处理事件) | `GET` | `/contract/api/contract/extraction-runs/{run_id}/events` |
| [重试失败阶段](contract.md#重试失败阶段) | `POST` | `/contract/api/contract/extraction-runs/{run_id}/stages/{stage_code}/retry` |
| [正式入库合同](contract.md#正式入库合同) | `POST` | `/contract/api/contract/extraction-runs/{run_id}/ingestion` |
| [获取合同注意事项列表](contract.md#获取合同注意事项列表) | `GET` | `/contract/api/contract/documents/{document_id}/notes` |
| [新增合同注意事项](contract.md#新增合同注意事项) | `POST` | `/contract/api/contract/documents/{document_id}/notes` |
| [获取合同内容摘要](contract.md#获取合同内容摘要) | `GET` | `/contract/api/contract/documents/{document_id}/summary` |
| [删除合同注意事项](contract.md#删除合同注意事项) | `DELETE` | `/contract/api/contract/documents/{document_id}/notes/{note_id}` |
| [创建合同关联](contract.md#创建合同关联) | `POST` | `/contract/api/contract/relations` |
| [删除合同关联](contract.md#删除合同关联) | `DELETE` | `/contract/api/contract/relations/{relation_id}` |
| [获取合同一跳关系列表](contract.md#获取合同一跳关系列表) | `GET` | `/contract/api/contract/documents/{document_id}/relations` |
| [读取合同 PDF](resource.md#读取合同-pdf) | `GET` | `/contract/api/resource/contract` |
| [读取提取任务的内存处理版 PDF](resource.md#读取提取任务的内存处理版-pdf) | `GET` | `/contract/api/resource/extraction-pdf/{file_id}` |
| [读取已驻留会话任务的 PDF 附件](resource.md#读取已驻留会话任务的-pdf-附件) | `GET` | `/contract/api/resource/conversations/{conversation_id}/files/{file_id}` |
| [创建临时展示任务](development-trace.md#创建临时展示任务) | `POST` | `/contract/api/communication/development-turns` |
| [读取开发链路](development-trace.md#读取开发链路) | `GET` | `/contract/api/communication/conversations/{conversation_id}/turns/{turn_id}/development-trace` |
| [获取当前用户会话列表](communication.md#获取当前用户会话列表) | `GET` | `/contract/api/communication/conversations` |
| [打开会话](communication.md#打开会话) | `POST` | `/contract/api/communication/conversations/{conversation_id}/open` |
| [向前刷新会话历史](communication.md#向前刷新会话历史) | `POST` | `/contract/api/communication/conversations/{conversation_id}/refresh` |
| [修改会话名称](communication.md#修改会话名称) | `PATCH` | `/contract/api/communication/conversations/{conversation_id}` |
| [删除会话](communication.md#删除会话) | `DELETE` | `/contract/api/communication/conversations/{conversation_id}` |
| [创建会话及首轮](communication.md#创建会话及首轮) | `POST` | `/contract/api/communication/conversations` |
| [创建或替换轮次](communication.md#创建或替换轮次) | `POST` | `/contract/api/communication/conversations/{conversation_id}/turns` |
| [订阅轮次事件流](communication.md#订阅轮次事件流) | `GET` | `/contract/api/communication/conversations/{conversation_id}/turns/{turn_id}/events` |
| [获取轮次快照](communication.md#获取轮次快照) | `GET` | `/contract/api/communication/conversations/{conversation_id}/turns/{turn_id}` |
| [取消轮次](communication.md#取消轮次) | `POST` | `/contract/api/communication/conversations/{conversation_id}/turns/{turn_id}/cancel` |

---

## 服务入口

本地默认服务地址为：

```text
http://127.0.0.1:10000
```

业务接口统一使用 `/contract/api` 前缀。当前自动生成的接口资料为：

| 入口 | 路径 |
| --- | --- |
| Swagger UI | `/docs` |
| ReDoc | `/redoc` |
| OpenAPI JSON | `/openapi.json` |

OpenAPI 是请求参数和响应 Schema 的机器可读来源；本目录中的 Markdown 负责跨接口业务语义、事件恢复和调用顺序。两者不一致时，应先修复代码契约，再同步文档。

---

## 全局约定

- 除二进制上传和流式响应外，数据均使用 JSON。
- 所有时间均为带时区的 ISO 8601 字符串。
- 资源不存在时返回 `404`，参数或请求体不符合 Schema 时返回 `422`。
- 应用主动返回的普通错误使用 `{"detail":"错误说明"}`。
- 自动生成结果未经专家确认，不能直接视为正式存储对象。

除 `GET /contract/api/health` 和 `POST /contract/api/auth/login` 外，所有接口必须通过标准请求头携带免登码：

```http
Authorization: Bearer <login_code>
```

免登码缺失、格式错误、无效或过期时统一返回 `401` 和 `{"detail":"免登码无效或已过期"}`。依赖校验成功后会向接口注入审核人名称，并把该免登码的过期点刷新为“当前时刻 + 配置 TTL”。合同任务以该名称绑定所有者，并在列表、快照、SSE 和状态变更时执行用户隔离；当前不区分角色，租户和 API 版本协商也尚未实现。

---

## 健康检查

```http
GET /contract/api/health
```

该接口只反映 API 进程是否存活，不探测 Elasticsearch、MLLM 或 Embedding 的连通性。

```json
{
  "status": "ok",
  "environment": "development"
}
```

---

## 业务接口

- [多轮对话 API](communication.md)：提供表单创建或替换、180 秒内订阅激活、SSE、快照和主动取消；工作流与上下文待接入。
- [审核用户登录 API](auth.md)：使用审核用户密钥获取限时免登码。
- [资源文件 API](resource.md)：根据 `file_uri` 读取正式合同 PDF，或按快照中的 UUID 读取本人提取任务的内存处理版 PDF。
- [合同 API](contract.md)：获取 Core 表单定义、列出并恢复未入库运行、上传 PDF、获取建议文件名与 Core/Clause 结果、订阅 SSE、重试失败阶段并提交正式入库。

新增业务接口时，应按资源或完整用例在本目录新增 kebab-case 文档，并同步更新[项目文档导航](../readme.md)。
