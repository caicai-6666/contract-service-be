# API 参考

> **用途：** 本页是外部 HTTP 与流式接口的统一入口，定义全局路径、媒体类型、错误格式和资源文档导航。

## 接口目录

按业务文档分组，点击接口名称可直接跳转到说明章节。

| 接口 | 方法 | 完整路径 |
| --- | --- | --- |
| [健康检查](#健康检查) | `GET` | `/knowledge-accumulation/contract/api/health` |
| [查询当前用户](auth.md#查询当前用户) | `GET` | `/knowledge-accumulation/contract/api/auth/me` |
| [获取所有已入库合同元数据](contract.md#获取所有已入库合同元数据) | `GET` | `/knowledge-accumulation/contract/api/contract/documents` |
| [获取全部文件等级变更记录](contract.md#获取全部文件等级变更记录) | `GET` | `/knowledge-accumulation/contract/api/contract/file-level-history` |
| [提交正式合同删除审核](contract.md#删除正式合同) | `DELETE` | `/knowledge-accumulation/contract/api/contract/documents/{document_id}` |
| [获取合同类别列表](contract.md#获取合同类别列表) | `GET` | `/knowledge-accumulation/contract/api/contract/categories` |
| [获取 Core 审核表单定义](contract.md#获取-core-审核表单定义) | `GET` | `/knowledge-accumulation/contract/api/contract/core-definitions` |
| [列出尚未入库的运行](contract.md#列出尚未入库的运行) | `GET` | `/knowledge-accumulation/contract/api/contract/extraction-runs` |
| [上传 PDF 并创建任务](contract.md#上传-pdf-并创建任务) | `POST` | `/knowledge-accumulation/contract/api/contract/extraction-runs` |
| [获取当前状态与提取结果](contract.md#获取当前状态与提取结果) | `GET` | `/knowledge-accumulation/contract/api/contract/extraction-runs/{run_id}` |
| [取消任务](contract.md#取消任务) | `DELETE` | `/knowledge-accumulation/contract/api/contract/extraction-runs/{run_id}` |
| [确认查重并继续](contract.md#确认查重并继续) | `POST` | `/knowledge-accumulation/contract/api/contract/extraction-runs/{run_id}/continue` |
| [订阅处理事件](contract.md#订阅处理事件) | `GET` | `/knowledge-accumulation/contract/api/contract/extraction-runs/{run_id}/events` |
| [重试失败阶段](contract.md#重试失败阶段) | `POST` | `/knowledge-accumulation/contract/api/contract/extraction-runs/{run_id}/stages/{stage_code}/retry` |
| [正式入库合同](contract.md#正式入库合同) | `POST` | `/knowledge-accumulation/contract/api/contract/extraction-runs/{run_id}/ingestion` |
| [获取合同注意事项列表](contract.md#获取合同注意事项列表) | `GET` | `/knowledge-accumulation/contract/api/contract/documents/{document_id}/notes` |
| [新增合同注意事项](contract.md#新增合同注意事项) | `POST` | `/knowledge-accumulation/contract/api/contract/documents/{document_id}/notes` |
| [获取合同内容摘要](contract.md#获取合同内容摘要) | `GET` | `/knowledge-accumulation/contract/api/contract/documents/{document_id}/summary` |
| [删除合同注意事项](contract.md#删除合同注意事项) | `DELETE` | `/knowledge-accumulation/contract/api/contract/documents/{document_id}/notes/{note_id}` |
| [创建合同关联](contract.md#创建合同关联) | `POST` | `/knowledge-accumulation/contract/api/contract/relations` |
| [删除合同关联](contract.md#删除合同关联) | `DELETE` | `/knowledge-accumulation/contract/api/contract/relations/{relation_id}` |
| [获取合同一跳关系列表](contract.md#获取合同一跳关系列表) | `GET` | `/knowledge-accumulation/contract/api/contract/documents/{document_id}/relations` |
| [获取待入库申请 ID 列表](ingestion-review.md#获取待入库申请-id-列表) | `GET` | `/knowledge-accumulation/contract/api/ingestion-reviews/list` |
| [获取待入库申请详情](ingestion-review.md#获取待入库申请详情) | `GET` | `/knowledge-accumulation/contract/api/ingestion-reviews/detail/{submission_id}` |
| [获取删除申请 ID 列表](deletion-review.md#获取删除申请-id-列表) | `GET` | `/knowledge-accumulation/contract/api/deletion-reviews/list` |
| [获取删除申请详情](deletion-review.md#获取删除申请详情) | `GET` | `/knowledge-accumulation/contract/api/deletion-reviews/detail/{submission_id}` |
| [读取合同 PDF](resource.md#读取合同-pdf) | `GET` | `/knowledge-accumulation/contract/api/resource/contract` |
| [读取提取任务的内存处理版 PDF](resource.md#读取提取任务的内存处理版-pdf) | `GET` | `/knowledge-accumulation/contract/api/resource/extraction-pdf/{file_id}` |
| [读取已驻留会话任务的 PDF 附件](resource.md#读取已驻留会话任务的-pdf-附件) | `GET` | `/knowledge-accumulation/contract/api/resource/conversations/{conversation_id}/files/{file_id}` |
| [创建临时展示任务](development-trace.md#创建临时展示任务) | `POST` | `/knowledge-accumulation/contract/api/communication/development-turns` |
| [读取开发链路](development-trace.md#读取开发链路) | `GET` | `/knowledge-accumulation/contract/api/communication/conversations/{conversation_id}/turns/{turn_id}/development-trace` |
| [获取当前用户会话列表](communication.md#获取当前用户会话列表) | `GET` | `/knowledge-accumulation/contract/api/communication/conversations` |
| [打开会话](communication.md#打开会话) | `POST` | `/knowledge-accumulation/contract/api/communication/conversations/{conversation_id}/open` |
| [向前刷新会话历史](communication.md#向前刷新会话历史) | `POST` | `/knowledge-accumulation/contract/api/communication/conversations/{conversation_id}/refresh` |
| [修改会话名称](communication.md#修改会话名称) | `PATCH` | `/knowledge-accumulation/contract/api/communication/conversations/{conversation_id}` |
| [删除会话](communication.md#删除会话) | `DELETE` | `/knowledge-accumulation/contract/api/communication/conversations/{conversation_id}` |
| [创建会话及首轮](communication.md#创建会话及首轮) | `POST` | `/knowledge-accumulation/contract/api/communication/conversations` |
| [创建或替换轮次](communication.md#创建或替换轮次) | `POST` | `/knowledge-accumulation/contract/api/communication/conversations/{conversation_id}/turns` |
| [订阅轮次事件流](communication.md#订阅轮次事件流) | `GET` | `/knowledge-accumulation/contract/api/communication/conversations/{conversation_id}/turns/{turn_id}/events` |
| [获取轮次快照](communication.md#获取轮次快照) | `GET` | `/knowledge-accumulation/contract/api/communication/conversations/{conversation_id}/turns/{turn_id}` |
| [取消轮次](communication.md#取消轮次) | `POST` | `/knowledge-accumulation/contract/api/communication/conversations/{conversation_id}/turns/{turn_id}/cancel` |

---

## 服务入口

本地默认服务地址为：

```text
http://127.0.0.1:20000
```

业务接口统一使用 `/knowledge-accumulation/contract/api` 前缀。当前自动生成的接口资料为：

| 入口 | 路径 |
| --- | --- |
| Swagger UI | `/knowledge-accumulation/docs` |
| ReDoc | `/knowledge-accumulation/redoc` |
| OpenAPI JSON | `/knowledge-accumulation/openapi.json` |

所有后端入口统一位于 `/knowledge-accumulation` 下，包括健康检查、HTTP、SSE、PDF 资源和上述文档入口。旧无应用前缀的地址不再挂载。网关应保留完整路径，前端需同步 API 基础地址；中间件的 `/middleware-service/api/...` 是独立上游协议，不受本前缀影响。

OpenAPI 是请求参数和响应 Schema 的机器可读来源；本目录中的 Markdown 负责跨接口业务语义、事件恢复和调用顺序。两者不一致时，应先修复代码契约，再同步文档。

---

## 全局约定

- 除二进制上传和流式响应外，数据均使用 JSON。
- 所有时间均为带时区的 ISO 8601 字符串。
- 资源不存在时返回 `404`，参数或请求体不符合 Schema 时返回 `422`。
- 应用主动返回的普通错误使用 `{"detail":"错误说明"}`。
- 自动生成结果未经专家确认，不能直接视为正式存储对象。

仅 `GET /knowledge-accumulation/contract/api/health` 公开。其他业务 API、资源和 SSE 请求均需 `Authorization: Bearer <用户 access_token>`，统一验证中间件登录有效且 userid 属于本平台管理员。无效用户返回401，无访问资格返回403，认证依赖不可用返回503。当前用户查询为 `GET /knowledge-accumulation/contract/api/auth/me`；本平台不再提供密钥登录或令牌签发。完整边界见[用户访问认证](auth.md)。

---

## 健康检查

```http
GET /knowledge-accumulation/contract/api/health
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

- [多轮对话 API](communication.md)：提供会话及任务创建、180 秒内订阅激活、业务门禁和 Agent Core、SSE、快照、主动取消与历史恢复。
- [用户访问认证 API](auth.md)：使用审核用户 userid获取限时用户访问令牌。
- [资源文件 API](resource.md)：根据 `file_uri` 读取正式合同 PDF，或按快照中的 UUID 读取本人提取任务的内存处理版 PDF。
- [合同 API](contract.md)：获取 Core 表单定义、列出并恢复未入库运行、上传 PDF、获取建议文件名与 Core/Clause 结果、订阅 SSE、重试失败阶段并提交正式入库。

新增业务接口时，应按资源或完整用例在本目录新增 kebab-case 文档，并同步更新[项目文档导航](../readme.md)。


待审数据、审核结果、清理计时与临时 PDF 查询见[待入库申请 API](ingestion-review.md)。

删除提交人或合同上传人查看相关删除申请、审核结果及实际删除状态，见[删除审核 API](deletion-review.md)。
