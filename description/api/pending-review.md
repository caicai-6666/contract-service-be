# 待入库申请 API

用于查看仍保留在待审区的申请、审核结果和临时 PDF。它与正式合同列表、内存提取任务列表分别管理；审核通过、审核拒绝的记录在清理之前都会出现在这里。

## 接口目录

| 接口 | 方法 | 完整路径 |
| --- | --- | --- |
| [获取清理配置](#获取清理配置) | GET | `/contract/api/pending-reviews/cleanup-policy` |
| [获取待入库申请 ID 列表](#获取待入库申请-id-列表) | GET | `/contract/api/pending-reviews/list` |
| [获取待入库申请详情](#获取待入库申请详情) | GET | `/contract/api/pending-reviews/detail/{submission_id}` |

PDF 预览已迁移至[资源文件 API](resource.md#读取待审-pdf)：`GET /contract/api/resource/pending-review-pdf/{submission_id}`，原待审路由下的 PDF 接口已移除。

所有接口使用 `Authorization: Bearer <免登码>`，未登录或令牌过期返回 401。系统用户权限统一，已登录用户可以查看全部仍保留的申请；不按提交人过滤。成功响应带 `Cache-Control: no-store`，文件路径不接受客户端自由指定。

---

## 获取清理配置

无请求参数。返回启动时实际采用的配置与服务器时间：

```json
{
  "retention_seconds": 604800,
  "cleanup_interval_seconds": 3600,
  "countdown_from": "review_processed_at",
  "server_time": "2026-10-09T08:00:00Z"
}
```

`retention_seconds` 对应 `PENDING_REVIEW_RETENTION_SECONDS`；`cleanup_interval_seconds` 对应后台扫描间隔。修改环境配置需重启后端。

前端优先使用详情的 `cleanup.eligible_at` 与服务器时间计算剩余时长：`max(0, cleanup.eligible_at - 当前服务器时间)`。只有“审核通过且正式入库成功”或“审核拒绝且本地处理完成”才开始计时。未完成、入库失败等情况为 null，不能按 created_at 或 reviewed_at 开始倒计时。

截止时刻表示允许清理，不保证该秒立刻删除：后台扫描、批次限制或清理失败重试都可能使实际删除更晚。倒计时为0但记录仍存在是合法状态，可展示“等待清理”。

---

## 获取待入库申请 ID 列表

`GET /contract/api/pending-reviews/list`，无参数、无分页。只返回所有尚未清理的申请 ID；不创建临时查询结果集，`submission_id` 就是用于查询详情的 ID。

```json
{
  "submission_ids": ["12345678-1234-4234-8234-123456789abc"]
}
```

按提交时间倒序、相同时间按申请 ID 升序排列。空列表返回 `{"submission_ids": []}`。包括待发送、待审核、已批准和已拒绝等所有仍保留记录。仅从 SQLite 读取 ID，不加载快照、合同内容或正式库数据。原根路径 `GET /contract/api/pending-reviews` 已移除。

---

## 获取待入库申请详情

`GET /contract/api/pending-reviews/detail/{submission_id}`，路径参数为列表返回的申请 UUID，不是合同 ID、通行证或中间件消息 ID。

详情采用稳定分区，各阶段尚未产生的值返回 null，不省略分区。下面为刚提交、尚未送审的示例：

```json
{
  "submission_id": "12345678-1234-4234-8234-123456789abc",
  "run_id": "提取任务标识",
  "document_id": "完整合同内容哈希",
  "file_name": "设备采购合同",
  "summary": "设备采购与交付约定。",
  "page_count": 3,
  "submitted_by": "Amy",
  "note": "请核对交付安排",
  "created_at": "2026-10-09T08:00:00Z",
  "updated_at": "2026-10-09T08:00:00Z",
  "delivery": {"status": "pending", "message_id": null},
  "review": {
    "status": "pending_send", "note": null, "reviewer": null,
    "reviewed_at": null, "passport": null
  },
  "ingestion": {"status": "pending", "ingested_at": null},
  "cleanup": {"review_processed_at": null, "eligible_at": null},
  "pdf": {
    "relative_path": "12345678-1234-4234-8234-123456789abc.pdf",
    "url": "/contract/api/resource/pending-review-pdf/12345678-1234-4234-8234-123456789abc"
  },
  "server_time": "2026-10-09T08:00:01Z"
}
```

| 分区或字段 | 契约 |
| --- | --- |
| 顶层基本信息 | 申请/任务/合同 ID、冻结的名称/摘要/页数、提交人和入库员 note；note 可以为空字符串 |
| created_at / updated_at | 保存待审申请时间、待审记录最近更新时间，不是正式入库时间 |
| delivery | status 为 pending / publishing / published / uncertain / blocked；message_id 为原送审消息 ID |
| review | status 为 pending_send / pending_review / approved / rejected；note 为审核平台备注，reviewer 和 reviewed_at 来自审核反馈 |
| review.passport | 批准时非空，同一值可关联多份合同；拒绝为空字符串；尚无反馈为 null |
| ingestion | status 为 pending / ingesting / succeeded / failed；ingested_at 从正式合同元数据读取，未成功或正式记录已不存在时为 null |
| cleanup | review_processed_at 为本地审核结果处理完成时间；eligible_at 为允许清理时刻，不保证该秒完成删除 |
| pdf | 始终返回相对待审 files 目录的文件名，以及需认证访问的资源接口 URL；不暴露服务器绝对路径 |
| server_time | 当前服务器时间，用于校正前端倒计时 |

状态组合的展示规则：

| 情况 | review.status | ingestion.status | cleanup.eligible_at |
| --- | --- | --- | --- |
| 尚未送审 | pending_send | pending | null |
| 等待审核 | pending_review | pending | null |
| 批准、入库尚未完成 | approved | pending / ingesting / failed | null |
| 批准、正式入库成功且本地处理完成 | approved | succeeded | 处理完成时间 + 保留时长 |
| 拒绝且本地处理完成 | rejected | pending | 处理完成时间 + 保留时长 |

审核反馈保存与本地处理完成存在短暂间隔；此时可已显示 approved/rejected，但处理完成时间及清理时刻仍为 null。以返回字段为准，不自行用提交时间或远端审核时间替代。

时间均为带时区的 ISO 8601 字符串。详情不返回 Core、Clause、向量、完整 snapshot_json 或内部错误日志。列表读取后申请可能被清理，因此详情返回404是正常边界。

| 状态码 | 含义 |
| --- | --- |
| 404 | 申请不存在或已清理（不会从最小幂等回执重建详情） |
| 422 | submission_id 不是合法 UUID |
| 503 | 列表或详情暂时无法读取，不伪装成空结果 |

---

## 实现与验证

路由为 `app/router/pending_review.py`，查询投影由 `PendingReviewQueryService` 负责，启动时注入待审存储、正式合同元数据存储和配置。列表仅查询 ID；详情从 snapshot_json 提取必要字段，不加载大型向量。两者均不触发发布、审核或入库。

`tests/test_pending_review_query.py` 覆盖认证、全量 ID 列表、详情各类状态、双方备注、正式入库时间、清理计时、清理后404、资源 PDF 成功/缺失/校验失败与错误隔离。生命周期规则见[待审清理服务](../capability/application/pending-review-cleanup.md)。
