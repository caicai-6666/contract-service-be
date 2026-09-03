# API 参考

> **用途：** 本页是外部 HTTP 与流式接口的统一入口，定义全局路径、媒体类型、错误格式和资源文档导航。

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

## 系统接口

### 健康检查

```http
GET /contract/api/health
```

该接口只反映 API 进程是否存活，不探测 Elasticsearch、MLLM、Embedding 或 Reranker 的连通性。

```json
{
  "status": "ok",
  "environment": "development"
}
```

---

## 业务接口

- [审核用户登录 API](auth.md)：使用审核用户密钥获取限时免登码。
- [资源文件 API](resource.md)：根据 Elasticsearch `file_uri` 读取本地正式合同 PDF。
- [合同 API](contract.md)：获取 Core 表单定义、列出并恢复未入库运行、上传 PDF、获取 Core/Clause 提取结果、订阅 SSE 和重试失败阶段。

新增业务接口时，应按资源或完整用例在本目录新增 kebab-case 文档，并同步更新[项目文档导航](../readme.md)。
