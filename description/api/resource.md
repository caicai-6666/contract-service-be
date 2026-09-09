# 资源文件 API

> **用途：** 本文定义正式合同 PDF 的磁盘读取，以及提取任务处理版 PDF 的内存读取。两者不涉及 Communication 会话附件。全局鉴权和错误约定见 [API 参考](readme.md)。

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

读取合同提取流程已经渲染、按视觉预算压缩并重新封装的 PDF，供刷新或重新打开提取任务时恢复预览。只允许当前任务所有者读取，三个权限等级均可访问本人的资源；UUID 不是免鉴权凭证。

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
