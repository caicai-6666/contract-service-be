# 合同提取 API

> **用途：** 本文是前端接入合同上传、处理进度、当前草稿和分支重试的 HTTP/SSE 参考。全局接口约定见[API 参考](readme.md)，内部状态机见[合同提取应用运行时](../architecture/contract-extraction-runtime.md)。

---

## 资源约定

- `run_id` 是一次上传任务的唯一标识，不等同于合同 `document_id`。
- `document_id` 是原始 PDF 字节的 SHA-256。
- SSE 只传状态事件；完整草稿只能通过快照接口获取。
- 原始 PDF、处理状态和草稿只在当前 API 进程内存中保留。

---

## 上传 PDF 并创建任务

```http
POST /contract/api/extraction-runs?file_name={file_name}
Content-Type: application/pdf
```

### 请求

| 位置 | 名称 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| Query | `file_name` | string | 是 | 前端展示使用的原始文件名，长度为 1～255。 |
| Body | PDF 字节 | binary | 是 | 非空的原始 PDF 请求体。 |

接口不接收服务器文件路径，也不使用 `multipart/form-data`。浏览器可以直接把 `File` 对象作为 `fetch` 的 `body`。

当前只实施文件名和请求体非空等最小检查。PDF 类型、大小、页数、加密、恶意内容和业务规则的正式上传校验尚未实现；无法读取的内容会在异步预处理时产生阶段失败事件。

### 示例

```bash
curl --request POST \
  --header 'Content-Type: application/pdf' \
  --data-binary '@contract.pdf' \
  'http://127.0.0.1:8080/contract/api/extraction-runs?file_name=contract.pdf'
```

### 成功响应

状态码：`202 Accepted`

响应是创建时刻的 `ContractExtractionSnapshot`。后台处理可能在响应后立即推进，因此前端应把该响应作为初始状态，并继续订阅 SSE 或查询快照。

```json
{
  "run": {
    "run_id": "f98a2b1d-...",
    "status": "processing",
    "created_at": "2026-08-29T10:00:00Z",
    "updated_at": "2026-08-29T10:00:00Z",
    "expires_at": "2026-08-29T11:00:00Z",
    "stages": {
      "document_reading": {
        "code": "document_reading",
        "name": "读取合同",
        "status": "pending",
        "message": "等待读取上传的 PDF。",
        "attempt": 0,
        "retryable": false,
        "progress": null,
        "result_status": null,
        "result_revision": null,
        "updated_at": "2026-08-29T10:00:00Z"
      }
    },
    "available_sections": []
  },
  "draft": null
}
```

实际 `stages` 始终包含六个阶段；示例只展开其中一个以说明结构。

---

## 获取当前状态与草稿

```http
GET /contract/api/extraction-runs/{run_id}
```

### 成功响应

状态码：`200 OK`

响应在同一聚合锁下生成，`run` 与 `draft` 属于同一时刻。`draft` 在第一个业务分支成功前为 `null`，之后包含分类和当前可用分区。

```yaml
run:
  run_id: f98a2b1d-...
  status: partial_ready
  created_at: "2026-08-29T10:00:00Z"
  updated_at: "2026-08-29T10:03:00Z"
  expires_at: "2026-08-29T11:03:00Z"
  stages: {}                     # 六个 StageSnapshot，以 code 为键
  available_sections: [core]
draft:
  revision: 1
  document:
    document_id: "原 PDF 字节 SHA-256"
    file_name: contract.pdf
    file_size_bytes: 102400
    page_count: 8
  classification:
    status: classified           # classified | unmapped | partial
    categories:
      - code: sale
        name: 买卖
        scenario: 采购机械臂设备
    unmapped_type_description: null
  core: {}                       # 可选 DraftSection<CoreDraftData>
  clause: null                   # 可选 DraftSection<ClauseDraftData>
  retrieval_view: null           # 可选 DraftSection<RetrievalViewDraftData>
  updated_at: "2026-08-29T10:03:00Z"
```

### 运行状态

| 值 | 含义 |
| --- | --- |
| `processing` | 尚无可用草稿，任务仍在处理。 |
| `partial_ready` | 至少一个分区可查看，其他分支仍在处理或失败。 |
| `ready` | 三个业务分支均已形成当前结果。 |
| `failed` | 公共处理失败，或三个业务分支均未形成结果。 |
| `expired` | 只会在到期 SSE 事件中观察到；随后查询返回 `404`。 |

### 阶段对象

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `code` | enum | 稳定机器标识。 |
| `name` | string | 非技术用户可读名称。 |
| `status` | enum | `pending`、`running`、`succeeded`、`failed` 或 `retrying`。 |
| `message` | string | 可直接展示给用户的简洁说明。 |
| `attempt` | integer | 已开始的尝试次数；未开始为 `0`。 |
| `retryable` | boolean | 当前状态是否允许提交独立重试。 |
| `progress` | object/null | 仅真实总量可知时包含 `completed` 和 `total`。 |
| `result_status` | enum/null | 已提交分区为 `completed` 或 `partial`。 |
| `result_revision` | integer/null | 当前成功分区的独立修订号。 |
| `updated_at` | datetime | 当前阶段最后更新时间。 |

### Core 分区

```yaml
revision: 1
result_status: completed         # completed | partial
updated_at: "2026-08-29T10:03:00Z"
data:
  fields:
    - name: 合同标的
      cardinality: multiple      # single | multiple
      status: extracted          # extracted | abandoned | failed
      property_names: [内容, 品牌, 型号, 数量, 单价]
      objects:
        - evidence:
            - page_number: 1
              content: "可核对的合同原文"
          reasoning: "面向审核者的简洁理由"
          value:                 # 只允许 string、integer、number、boolean
            内容: 机械臂
            数量: 2
      reasoning: null            # abandoned 时填写
      message: null              # failed 时填写用户提示
```

`abandoned` 的 `objects` 必须为空并提供 `reasoning`；`failed` 可以保留已通过校验的部分对象，但只通过 `message` 解释当前失败，不暴露内部异常。

### Clause 分区

```yaml
revision: 1
result_status: partial
updated_at: "2026-08-29T10:04:00Z"
data:
  clauses:
    - candidate_id: clause-0001
      order: 1
      identifier: "第一条"
      title_hint: 合同标的
      document_path:
        - identifier: "第一条"
          title_hint: 合同标的
      parent_candidate_id: null
      level: 1
      evidence:
        start:
          page_number: 1
          anchor: "第一条 合同标的"
        end:
          page_number: 1
          anchor: "……以验收单为准。"
      status: extracted          # extracted | failed
      reasoning_summary: "已核对起止锚点与直接正文。"
      content: "第一条 合同标的……"
      message: null              # failed 时填写，正文与理由为 null
```

### Retrieval 分区

```yaml
revision: 1
result_status: completed
updated_at: "2026-08-29T10:05:00Z"
data:
  questions:
    - question_id: generated-question-0001
      order: 1
      question: "这批机械臂要在什么时候交付？"
  vector_ready: true
  vector_dimensions: 4096
  source_question_count: 8
```

快照不返回逐问题向量或合同融合向量。

### 错误响应

| 状态码 | 条件 |
| --- | --- |
| `404` | `run_id` 不存在或任务已经从内存释放。 |

---

## 订阅处理事件

```http
GET /contract/api/extraction-runs/{run_id}/events
Accept: text/event-stream
Last-Event-ID: 11
```

`Last-Event-ID` 可省略。提供时必须是非负整数，服务只回放缓冲区中 `sequence` 更大的事件，然后继续发送实时事件。

### 业务事件帧

```text
id: 12
event: stage.completed
data: {"sequence":12,"run_id":"...","event_type":"stage.completed","overall_status":"processing","message":"合同类型已识别。","stage":{...},"draft_revision":null,"available_sections":[],"occurred_at":"2026-08-29T10:02:00Z"}

```

`event` 与 JSON 中的 `event_type` 保持一致。业务事件包括：

| 事件 | 前端动作建议 |
| --- | --- |
| `run.started` | 初始化任务时间线。 |
| `stage.started` | 展示当前阶段正在处理。 |
| `stage.progress` | 更新阶段消息或真实离散进度。 |
| `stage.completed` | 标记阶段成功。 |
| `stage.failed` | 展示失败消息，并依据 `retryable` 决定是否显示重试。 |
| `stage.retrying` | 标记已接受重试。 |
| `draft.updated` | 调用快照接口获取最新完整草稿。 |
| `run.review_ready` | 首份草稿已可查看。 |
| `run.expired` | 结束订阅并提示重新上传。 |

### 心跳帧

```text
event: heartbeat
data: {"run_id":"...","occurred_at":"2026-08-29T10:02:15Z"}

```

心跳没有 `id`，不进入业务事件序列，也不触发草稿刷新。

### 响应头

服务返回：

```text
Content-Type: text/event-stream
Cache-Control: no-cache, no-transform
X-Accel-Buffering: no
```

若断线时间超过事件缓冲范围，前端应先请求一次全量快照恢复权威状态，再携带最新已处理事件序号重新订阅。

### 错误响应

| 状态码 | 条件 |
| --- | --- |
| `400` | `Last-Event-ID` 不是非负整数。 |
| `404` | `run_id` 不存在或任务已经过期。 |

---

## 重试业务分支

```http
POST /contract/api/extraction-runs/{run_id}/stages/{stage_code}/retry
```

`stage_code` 只接受：

- `core_extraction`；
- `clause_extraction`；
- `retrieval_preparation`。

成功接受后返回 `202 Accepted` 和当前快照，目标阶段已经是 `retrying`。旧成功分区仍会出现在响应中；只有新尝试成功后，其修订号和内容才会原子替换。

### 错误响应

| 状态码 | 条件 |
| --- | --- |
| `404` | `run_id` 不存在或任务已经过期。 |
| `409` | 公共上下文未完成、阶段正在运行，或已经达到最大尝试次数。 |
| `422` | `stage_code` 不是三个可重试阶段之一。 |

---

## 推荐前端调用顺序

1. 使用原始 PDF 请求体创建任务并保存 `run_id`。
2. 立即渲染创建响应中的初始阶段状态。
3. 建立 SSE 连接，按事件更新处理时间线。
4. 收到 `draft.updated` 或 `run.review_ready` 后获取一次全量快照。
5. 对 `retryable: true` 的失败业务阶段提供单独重试入口。
6. 重试成功后再次根据 `draft.updated` 获取最新修订。
7. 收到 `run.expired`、用户离开审核流程或最终确认完成后关闭 SSE。

> **当前边界：** 专家编辑、最终确认和 Elasticsearch 入库接口尚未实现，不应由前端自行拼接临时写入请求。
