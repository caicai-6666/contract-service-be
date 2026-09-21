# 资源文件 API

> **用途：** 本文定义正式合同 PDF、提取任务处理版 PDF，以及已驻留会话任务附件的读取接口。三者的标识与授权边界不同。全局鉴权和错误约定见 [API 参考](readme.md)。

## 接口目录

| 接口 | 方法 | 完整路径 |
| --- | --- | --- |
| [读取合同 PDF](#读取合同-pdf) | `GET` | `/contract/api/resource/contract` |
| [读取提取任务的内存处理版 PDF](#读取提取任务的内存处理版-pdf) | `GET` | `/contract/api/resource/extraction-pdf/{file_id}` |
| [读取已驻留会话任务的 PDF 附件](#读取已驻留会话任务的-pdf-附件) | `GET` | `/contract/api/resource/conversations/{conversation_id}/files/{file_id}` |

---

## 读取合同 PDF

```http
GET /contract/api/resource/contract?file_uri=%2F<document_id>.pdf
Authorization: Bearer <login_code>
```

`file_uri` 直接使用 SQLite 正式合同目录或 Elasticsearch 候选文档中的同名字段，两处值一致。当前本地文件协议只接受根相对的单层 PDF 地址，例如 `/14db0d...ad3fe.pdf`；不接受外部 URL、相对路径、子目录、查询参数、片段或目录穿越。

成功时返回 `200 OK`，媒体类型为 `application/pdf`，响应体是对应 PDF 的二进制内容。响应使用 `Content-Disposition: inline` 和 `Cache-Control: private, no-store`，便于前端预览且不由共享缓存保存。

最小请求示例：

```bash
curl --get 'http://127.0.0.1:10000/contract/api/resource/contract' \
  --header 'Authorization: Bearer <login_code>' \
  --data-urlencode 'file_uri=/14db0d5691dc171e9288bff1296026d9915b6a2df0fc6870a744217cda7ad3fe.pdf' \
  --output contract.pdf
```

---

## 错误响应

| 状态码 | 触发条件 |
| --- | --- |
| `400` | `file_uri` 不符合本地合同文件协议或试图逃逸合同目录。 |
| `401` | Bearer 免登码缺失、无效或已经过期。 |
| `404` | 地址合法，但 `data/contract` 中不存在对应 PDF。 |
| `422` | 未提交 `file_uri`，或参数长度不符合接口 Schema。 |

接口只负责读取磁盘文件，不查询或修改 Elasticsearch，也不根据展示用 `file_name` 定位文件。SQLite 文件地址见[合同 SQLite 元数据结构](../architecture/data/contract-sqlite-metadata.md)，本地处理版 PDF 与完整内容的关联契约见[合同 Elasticsearch 文档结构](../architecture/data/contract-elasticsearch-document.md)。

---

## 读取提取任务的内存处理版 PDF

### 方法与用途

```http
GET /contract/api/resource/extraction-pdf/{file_id}
Authorization: Bearer <login_code>
```

读取合同提取流程已经渲染、按视觉预算压缩并重新封装的 PDF，供刷新或重新打开提取任务时恢复预览。只允许当前任务所有者读取，所有已登录用户均可访问本人的资源；UUID 不是免鉴权凭证。

### 请求参数

| 参数 | 位置 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file_id` | path | 是 | 合法 UUID，取创建响应或单任务快照中的 `run.document.file_id`，也可取运行列表项的 `document.file_id`。 |
| `Authorization` | header | 是 | 当前用户的 Bearer 免登码。 |

无查询参数及请求体。一轮提取只持有一份处理版 PDF，`file_id` 复用该任务的 `run_id`；不是正式合同的 SHA-256 `document_id`，也不是磁盘 `file_uri`。

### 成功响应

返回 `200 application/pdf`，响应体是内存处理版 PDF 字节，不内联在 JSON 或 SSE 中。响应携带 `Content-Disposition: inline`、`Content-Length` 和 `Cache-Control: private, no-store`。当前返回完整文件，不提供 HTTP Range 分段响应；前端可携带鉴权请求获取 Blob 后交给 PDF 阅读器。

### 错误响应

| 状态码 | 触发条件 |
| --- | --- |
| `401` | 未登录、免登码无效或过期。 |
| `404` | UUID 不存在、任务不属于当前用户，或任务已取消、过期、成功入库并释放。 |
| `422` | `file_id` 不是合法 UUID。 |

### 请求示例

```bash
curl --header 'Authorization: Bearer <login_code>' \
  'http://127.0.0.1:20000/contract/api/resource/extraction-pdf/5a31a5f0-714d-4d46-8455-3f9643c8e9a7' \
  --output processed-contract.pdf
```

示例 UUID 必须替换成快照实际返回值。

### 行为与边界

- 使用任务内同一份页面 PNG 按需组装 PDF，不重新渲染；组装后校验创建时的文件大小和 SHA-256。不新增 PDF 缓存、文件索引或磁盘副本，读取不会更新任务的 `updated_at`、普通 TTL 或查重确认期限。
- 组装在线程中执行，不占用任务聚合锁；组装完成后再次检查生命周期，期间被取消或释放则返回 `404`。多次读取返回相同字节，但每次需要重新封装；前端宜复用已取得的 Blob。
- 资源与任务同生命周期。失败、等待确认、非合同或重复拒绝不单独删除 PDF，任务仍驻留且未过期时可读；取消、过期回收、成功入库及进程重启后失效。正在执行的任务沿用既有 TTL 规则，不因读取被提前终止。
- 读取授权与任务状态检查在聚合锁内完成，传输不占用聚合锁。已获授权的在途响应可持有字节直到结束；任务释放后的新请求返回 `404`。
- 入库成功后应改用正式合同的 `file_uri` 调用上面的磁盘读取接口，不再依赖临时 UUID。
- 栅格化处理版不保留原 PDF 的文本层、表单、批注等结构，也不保证字节数更小，详见 [PDF 视觉压缩与重新封装](../capability/document/pdf-page-compression.md)。

---

## 读取已驻留会话任务的 PDF 附件

### 方法与用途

```http
GET /contract/api/resource/conversations/{conversation_id}/files/{file_id}
Authorization: Bearer <login_code>
```

读取当前用户已经加载到内存的任务轨迹中、获准使用的原始上传 PDF。优先返回内存字节；内存字节已释放时读取 `data/communication/upload` 中对应文件。无需等待任务成功或落盘。

### 认证方式

使用登录接口取得的 Bearer 免登码。所有已登录用户均可读取本人附件，所有权按用户 `secret_key` 校验，不按展示用户名匹配；UUID 本身不是访问凭证。

### 请求参数

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `conversation_id` | path | string | 是 | 文件所属会话 ID，长度 1～128，须属于当前用户且已驻留。 |
| `file_id` | path | UUID | 是 | 会话任务轨迹 `input.files` 中的 `file_id`。不是合同 `document_id` 或提取任务 `run_id`。 |
| `Authorization` | header | string | 是 | `Bearer <login_code>`。 |

无查询参数及请求体。不接受文件路径、展示名或原始文件名作为读取地址。

### 成功响应

`200 OK`，媒体类型 `application/pdf`，响应体为完整 PDF 二进制，不是 JSON 或 Base64。携带以下响应头：

```http
Content-Type: application/pdf
Content-Disposition: inline; filename*=UTF-8''...
Content-Length: <文件字节数>
Cache-Control: private, no-store
X-Content-Type-Options: nosniff
```

文件名采用原始上传名称进行安全编码。前端可携带鉴权请求取得 Blob 后预览；当前不提供 HTTP Range 分段响应。

### 错误响应

| 状态码 | 触发条件 |
| --- | --- |
| `401` | 免登码缺失、无效或过期。 |
| `404` | 文件不存在、非本人、所属任务轨迹尚未加载或已驱逐、附件未准入、任务拒绝/过期，或内存与安全磁盘文件均不可用。 |
| `422` | `conversation_id` 长度不合法或 `file_id` 不是合法 UUID。 |

所有不可访问情况统一返回 `{"detail":"会话附件不存在或不可用"}`，不暴露其他用户文件、路径或磁盘存在性。

### 请求示例

```bash
curl --header 'Authorization: Bearer <login_code>' \
  'http://127.0.0.1:20000/contract/api/resource/conversations/<conversation_id>/files/5a31a5f0-714d-4d46-8455-3f9643c8e9a7' \
  --output conversation-attachment.pdf
```

将会话 ID 和示例 UUID 替换为会话轨迹实际返回的值。

### 行为与边界

- 必须同时满足：当前用户所有、所属任务轨迹已驻留、轨迹包含该 `file_id`、`admission=accepted`、路径符合 `/<file_id>.pdf`。不按磁盘文件是否存在推断授权。
- 使用 `conversation_id` 直接定位指定驻留会话，仅搜索该会话的任务轨迹，不遍历其他会话。即使另一会话同属当前用户，文件与请求会话不匹配也返回 `404`。不保留只传 `file_id` 的旧路径。
- **会话已驻留不等于全部任务已加载。** 若附件属于最新摘要之前、尚未加载的旧任务，即使知道 UUID 且磁盘已有 PDF，仍返回 `404`。前端应通过会话 `refresh` 加载对应任务，再请求本接口；接口自身不查询 SQLite 历史、不自动 open/refresh。
- 已准入任务在 `processing`、`completed`、`cancelled`、`superseded`、`failed` 状态均可读取。待准入、未激活、拒绝、过期任务不可读取；缺少明确 `accepted` 的旧记录不默认放行。
- 返回原始上传 PDF，不是页面渲染后重新封装的处理版。读取不调用模型、不重新渲染、不触发落盘、不新增缓存副本，也不改变轨迹或任务状态。
- 成功读取视为用户活动，刷新该会话空闲计数，防止仍在预览的会话被当作无活动驱逐；不会延长任务激活期限。
- 磁盘读取在线程中执行，不持有会话锁；只读取 UUID 对应的普通非空文件，拒绝符号链接、目录及特殊文件。读取结束后再次检查任务授权与同一次会话驻留身份。
- 会话删除或驱逐后，新请求立即失去访问权，磁盘残留文件也不可访问。已完成授权、开始传输的响应可以持有字节直到结束，不持有会话锁；无法撤回用户已经下载的内容。
- 实现位于 `resource.get_communication_pdf`、`ConversationHistoryService.read_file` 和 `communication_files.read_uploaded_pdf`。测试见 `tests/test_communication_pdf_resource.py`，覆盖 HTTP 鉴权、内存读取、跨摘要 refresh、磁盘安全和驱逐竞态。
