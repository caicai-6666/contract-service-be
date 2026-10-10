# 中间件平台会话

本模块在后端启动后异步对接中间件，以平台身份登录并定期保活。中间件连接失败不阻止应用正常启动和业务处理。

---

## 职责与生命周期

- `app/infrastructure/middleware.py`：HTTP 客户端、登录与心跳响应校验。
- `app/service/middleware_session.py`：单个后台任务管理令牌、心跳与重新登录。
- `app/bootstrap.py`：通过 lifespan 启动服务，保存到 `application.state.middleware_session_service`；退出时取消在途请求或等待任务，清除内存令牌并关闭客户端。

地址、平台编码或 secret 未完整填写时不连接；配置加载后固定，填写或修改后需重启。启动方法只派发后台作业，不等待登录结果。令牌仅保存于进程内存，不落盘，不向前端暴露。

---

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MIDDLEWARE_BASE_URL` | 空 | HTTP/HTTPS 服务根地址，包含 IP 和端口，不附加接口路径。正式部署使用 HTTPS。 |
| `MIDDLEWARE_PLATFORM_CODE` | 空 | 中间件登记的平台代号，符合 `^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$`。 |
| `MIDDLEWARE_SECRET` | 空 | 对应该平台的登录凭证，保留原值，按敏感字段保存。 |
| `MIDDLEWARE_RETRY_INTERVAL_SECONDS` | `30` | 失败后的重试间隔，有限正数，单位秒。 |
| `MIDDLEWARE_REQUEST_TIMEOUT_SECONDS` | `10` | 单次请求总超时，有限正数，单位秒。 |

客户端禁用环境代理与自动重定向，直接访问所配置的服务。配置入口为 `app/core/config.py`，模板为 `.env.example`。

---

## 请求与状态转换

登录使用 `POST /middleware-service/api/platform/login`，JSON 请求体仅包含 `platform_code` 和 `secret`。成功响应必须包含非空 `access_token`、`token_type=bearer`、匹配的平台 `code`、正整数有效期及心跳间隔，且间隔小于有效期。

心跳使用 `POST /middleware-service/api/platform/heartbeat`，携带 `Authorization: Bearer <access_token>`，JSON 请求体仅包含 `platform_code`。成功后更新有效期和下次心跳间隔，令牌保持不变。间隔采用每次响应值，不固定为本地重试间隔。

本地以请求开始时的单调时间加有效期保守估计失效时刻；网络耗时不会延长本地判断的有效期。

| 情况 | 行为 |
| --- | --- |
| 登录失败、网络异常、超时或非法响应 | 延迟后重试登录，不影响业务启动。 |
| 登录返回 409 | 等待重试，不尝试踢出已有会话；令牌丢失时等待服务端旧会话过期。 |
| 心跳返回 401 | 清除令牌，等待重试间隔后重新登录。 |
| 心跳临时失败 | 有效期内保留令牌并重试心跳；达到本地失效时刻改为登录。 |
| 系统关闭 | 取消后台任务并等待清理，随后关闭 HTTP 连接池。 |

日志仅记录阶段、异常类型和 HTTP 状态，不输出凭据、令牌或响应正文。当前接口无登出能力，关闭后服务端会话自然到期；立即重启可能暂时收到 409。

---

## 部署与验证

同一平台仅允许一个活跃会话。遵循项目现有单 worker 运行要求，多个进程或实例共用 platform_code 会发生登录竞争，不能借由本服务绕过中间件的唯一会话限制。

本地验证使用 HTTP 模拟响应和可控时间，不依赖实际中间件：

```bash
PYTHONPATH=.:tests python -m unittest test_middleware_session -q
```

覆盖请求契约、响应校验、后台启动、409 重试、401 重新登录、网络故障后保留令牌、过期重登、服务端间隔更新及关闭取消。正式连通性需填写实际服务地址和凭据后验证。


---

## 待审消息发送接入

[入库待审请求发布服务](../application/ingestion-review-publisher.md)与[删除审核发布服务](../application/deletion-review-publisher.md)复用本服务的有效令牌，使用独立客户端上传文件。收到 401 时仅失效该请求使用的令牌；心跳和重新登录仍由本服务统一管理，不记录令牌明文。
