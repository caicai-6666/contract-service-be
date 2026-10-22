# 用户访问认证 API

本平台不再验证本地密钥或签发免登码。用户由统一登录平台完成登录，随后携带用户 access_token 访问本平台；身份及当前平台管理员资格由中间件验证。实现及迁移见[用户身份与所有权](../architecture/data/user-identity.md)。

## 接口目录

| 接口 | 方法 | 完整路径 |
| --- | --- | --- |
| [查询当前用户](#查询当前用户) | `GET` | `/knowledge-accumulation/contract/api/auth/me` |

---

## 查询当前用户

必填请求头 `Authorization: Bearer <用户 access_token>`，无请求体及查询参数。该接口与其他业务接口使用相同认证依赖，查询会续期已有有效的用户登录，不创建登录、不复活过期 token。

成功返回 `200 application/json`，带 `Cache-Control: no-store`：

```json
{"userid":"user-001","username":"张三","ttl":90}
```

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| userid | string | 中间件返回的稳定用户 ID，作为会话、提取任务和删除审核人员范围的依据。 |
| username | string | 当前显示名；业务操作时可冻结为上传人、申请人等名称快照，不用于身份匹配。 |
| ttl | integer | 有效登录查询后续期的完整秒数，由中间件决定，不是本平台缓存期限。 |

```bash
curl 'http://127.0.0.1:20000/knowledge-accumulation/contract/api/auth/me' \
  -H 'Authorization: Bearer replace-with-user-access-token'
```

---

## 放行条件与错误

独立依赖 `app/router/dependency.py:require_platform_user` 顺序执行：

1. 解析来访用户的 Bearer token。
2. 用后台维护的平台令牌调用 `POST /middleware-service/api/user/login-cache`，JSON 只提交用户 access_token；响应必须登录有效并含 userid、username 和正数 TTL。
3. 用平台令牌调用 `GET /middleware-service/api/platform/admins/list`；用户 userid 必须在当前平台管理员列表中。
4. 注入不含凭据的 `AuthenticatedUser`。同一请求的多个依赖复用验证结果；不同请求重新查询，不跨请求缓存登录或管理员资格。

| 状态码 | 含义 |
| --- | --- |
| 401 | 用户 token 缺失、格式错误，或中间件以 200 返回登录已失效；携带 WWW-Authenticate: Bearer。 |
| 403 | 用户登录有效，但不属于当前平台管理员。 |
| 503 | 平台会话未就绪、远端请求失败/超时或响应非法；不据此认定用户退出，也不按旧本地用户配置放行。 |

中间件自身返回401表示平台令牌失效，失效该平台令牌并等待后台重登；向前端返回503，不混淆为用户 token 失效。错误不回显任何令牌或远端正文。

---

## 前端与连接边界

- 移除旧 `POST /knowledge-accumulation/contract/api/auth/login` 调用与本地密钥登录页面，使用统一登录平台提供的用户 token。旧接口不保留兼容入口。
- 所有业务 API、PDF 资源和 SSE 新连接都携带同一用户 Bearer token。仅健康检查公开；`/auth/me` 也必须验证管理员资格。
- SSE 在建立连接时验证，已建立连接不会逐帧重新查询中间件；SSE 心跳不是用户登录续期。重连、快照和后续操作再次验证。若前端需要持续保持活跃登录，可在确有用户活动时调用 /auth/me，间隔应小于中间件返回 TTL；不由后台任务无限续期用户。
- 本平台不写中间件用户总表、不创建用户登录缓存，也不维护管理员列表。平台登录心跳与用户登录生命周期独立。
