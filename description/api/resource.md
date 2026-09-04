# 资源文件 API

> **用途：** 本文定义前端根据 SQLite 正式合同目录中的 `file_uri` 读取 `data/contract` 下 PDF 的接口。全局鉴权和错误约定见 [API 参考](readme.md)。

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
