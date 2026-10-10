# 合同删除审核 API

查询删除审核区保存的申请和结果，路由前缀为 `/contract/api/deletion-reviews`。采用与入库审核一致的“申请 ID 列表、按 ID 查询详情”方式，无分页；已完成申请在配置的保留期之后自动清理。

提交入口为[合同删除审核申请](contract.md#删除正式合同)：`DELETE /contract/api/contract/documents/{document_id}`，202响应返回本页使用的 submission_id。

## 接口目录

| 接口 | 方法 | 完整路径 |
| --- | --- | --- |
| [获取删除申请 ID 列表](#获取删除申请-id-列表) | `GET` | `/contract/api/deletion-reviews/list` |
| [获取删除申请详情](#获取删除申请详情) | `GET` | `/contract/api/deletion-reviews/detail/{submission_id}` |

人员条件由服务端登录身份决定：`requested_by = 当前登录用户 OR uploader = 当前登录用户`。两个角色都匹配时仅返回一次；外部审核人不是查询范围的判定依据。使用申请时冻结的人员信息，正式合同删除后仍可读取审核结果。

---

## 获取删除申请 ID 列表

`GET /contract/api/deletion-reviews/list`，返回当前用户作为删除提交人或原合同上传人所涉及的全部保留申请 ID。

**认证方式：** `Authorization: Bearer <免登码>`，服务端读取用户名称作为筛选值。

**请求参数：** 无 path、query 参数，无请求体。人员名称不由客户端提交；无分页。

**成功响应：** `200 OK`，`application/json`，带 `Cache-Control: no-store`。

```json
{
  "submission_ids": ["12345678-1234-4234-8234-123456789abc"]
}
```

**错误响应：**

| 状态码 | 含义 |
| --- | --- |
| 401 | 免登码缺失、无效或过期。 |
| 503 | 数据库读取失败；不伪装为空列表。 |

**请求示例：** 将免登码替换为登录接口返回值。

```bash
curl 'http://127.0.0.1:20000/contract/api/deletion-reviews/list' \
  -H 'Authorization: Bearer replace-with-login-code'
```

**行为与边界：** 按创建时间倒序、同一时间按申请 ID 升序排列。空结果为 `{"submission_ids": []}`；包括待发送、待审核、批准、拒绝以及实际删除成功或失败的保留申请。列表只读取 ID，不创建查询缓存，不触发送审或删除。

---

## 获取删除申请详情

`GET /contract/api/deletion-reviews/detail/{submission_id}`，返回指定申请的冻结合同信息、双方备注、审核结果和实际删除状态。

**认证方式：** `Authorization: Bearer <免登码>`。与列表使用相同的人员 OR 条件；不允许通过猜测 UUID 查看无关申请。

**请求参数：**

| 位置 | 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| path | submission_id | UUID | 是 | 列表返回的删除申请 ID；不是 document_id、passport 或消息 ID。 |

无 query 参数，无请求体。

**成功响应：** `200 OK`，`application/json`，带 `Cache-Control: no-store`。分区固定，尚未产生的可选内容返回 null。

以下为已批准并完成正式删除的示例：

```json
{
  "submission_id": "12345678-1234-4234-8234-123456789abc",
  "document_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "passport": "passport-1",
  "file_name": "设备采购合同",
  "summary": "约定设备采购和交付验收。",
  "uploader": "Amy",
  "requested_by": "Bob",
  "contract_ingested_at": "2026-10-09T08:00:00Z",
  "note": "重复资料，请核对后删除",
  "created_at": "2026-10-10T01:00:00Z",
  "updated_at": "2026-10-10T02:00:30Z",
  "delivery": {"status": "published", "message_id": "request-1"},
  "review": {
    "status": "approved", "note": "同意删除", "reviewer": "张三",
    "reviewed_at": "2026-10-10T02:00:00Z"
  },
  "deletion": {"status": "succeeded", "completed_at": "2026-10-10T02:00:30Z"},
  "cleanup": {
    "review_processed_at": "2026-10-10T02:00:30Z",
    "eligible_at": "2026-10-17T02:00:30Z"
  },
  "pdf": {
    "file_uri": "/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.pdf",
    "url": null
  },
  "server_time": "2026-10-10T02:01:00Z"
}
```

| 字段或分区 | 契约 |
| --- | --- |
| 合同基本信息 | document_id、passport、名称、摘要、上传人、原入库时间来自申请时的快照；历史摘要或通行证可为 null。 |
| requested_by / note | 删除提交人及其备注，与外部审核人和审核备注分别保存。 |
| created_at / updated_at | 申请创建时间、送审回执/审核/删除执行的最近更新时间。 |
| delivery | status 为 pending / publishing / published / uncertain / blocked；只有 published 表示已确认送审，不表示审核通过；uncertain 表示结果未确认，后台按相同 source_id 定时重试；合法409重复回执关联原消息并转为 published。 |
| review | status 为 pending_send / pending_review / approved / rejected；note、reviewer、reviewed_at 来自外部审核结果，未产生时为 null。 |
| deletion | status 为 pending / deleting / succeeded / failed；只有 succeeded 表示实际删除已完成，completed_at 在此时非空。 |
| cleanup | review_processed_at 为本地审核处理完成时间：批准时正式删除完成，拒绝时标志协调完成；eligible_at 为该时间加保留期，未完成为 null。 |
| pdf | file_uri 始终保留原地址；原合同身份仍匹配且 PDF 存在时，url 为 `/contract/api/resource/contract?file_uri=...`，需登录；删除完成、文件缺失或原身份不匹配时为 null。 |
| server_time | 当前服务器时间，带时区的 ISO 8601。 |

**错误响应：**

| 状态码 | 含义 |
| --- | --- |
| 401 | 免登码缺失、无效或过期。 |
| 404 | 申请不存在、已清理，或当前用户既非删除提交人也非合同上传人；统一响应，不区分原因。 |
| 422 | submission_id 不是合法 UUID。 |
| 503 | 详情或所依赖的正式存储暂时无法读取。 |

**请求示例：** 将免登码和申请 UUID 替换为实际值。

```bash
curl 'http://127.0.0.1:20000/contract/api/deletion-reviews/detail/12345678-1234-4234-8234-123456789abc' \
  -H 'Authorization: Bearer replace-with-login-code'
```

前端直接使用详情的 `cleanup.eligible_at` 与 `server_time` 校正后的当前服务器时间计算 `max(0, eligible_at - 当前服务器时间)`。`eligible_at` 为 null 时不显示倒计时，无需额外查询清理配置。截止时刻表示允许清理，扫描批次或失败重试可能使实际清理更晚；倒计时为0但记录仍存在是合法情况。

**行为与边界：** approved 与 pending/deleting/failed 可以同时出现，不能把批准等同于删除成功。拒绝通常仍为 deletion.status=pending；拒绝反馈保存与标志恢复有短暂间隔，此时 review_processed_at 和 eligible_at 仍可为 null。计时不能用申请创建时间、远端审核时间或 updated_at 替代。清理后申请从列表消失，详情返回404，不从最小回执恢复历史内容。

发送状态见[删除审核后台发布](../capability/application/deletion-review-publisher.md)。详情不公开发送尝试次数、重试计划或内部错误。

无完整快照、内部重试计划、异常内容或服务器绝对路径返回。PDF 预览检查与后续资源请求之间可能发生正式删除，此时预览请求返回 404。同一 document_id 后来重新入库时，不为旧申请生成新版 PDF 的预览 URL。

---

## 实现与验证

`app/router/deletion_review.py` 负责接口与认证身份注入，`DeletionReviewQueryService` 负责只读投影。SQLite 在参数化查询中执行人员 OR 筛选，列表与详情共用范围；后台删除状态见[删除审核存储](../architecture/data/deletion-review.md)和[批准删除执行器](../capability/application/deletion-review-executor.md)。

启动时将查询服务注入 `app.state.deletion_review_query_service`，复用正式元数据、本地 PDF 存储与保留配置，不依赖 ES、Neo4j 或模型请求。`tests/test_deletion_review_query.py` 覆盖人员角色与去重、详情范围、认证、空列表、各阶段状态、双方备注、PDF 缺失与重新入库、查询错误隔离和读取不修改记录；`tests/test_deletion_review_cleanup.py` 验证详情倒计时、清理后404和已移除的清理策略接口，完整生命周期见[删除审核清理服务](../capability/application/deletion-review-cleanup.md)。
