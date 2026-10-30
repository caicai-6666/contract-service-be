# 用户身份与所有权

主分支采用中间件统一用户身份。访问必须同时满足有效用户登录和当前平台管理员成员资格；不从用户整数等级推断管理员，也不在本轮引入文件级别规则。HTTP 契约见[用户访问认证](../../api/auth.md)。

---

## 职责与生命周期

| 位置 | 职责 |
| --- | --- |
| app/infrastructure/middleware.py | 严格解析用户登录查询与平台管理员列表，分别传递用户 token 和平台 Bearer。 |
| app/service/auth.py | 复用平台会话，完成两次远端查询；区分未登录、无访问资格和依赖故障，不签发、不持久化、不缓存用户令牌。 |
| app/router/dependency.py | 独立 FastAPI 验证依赖及userid/username/当前部门资料注入。路由聚合统一保护业务入口，同一请求复用结果。 |
| app/schema/auth.py | 不可变 AuthenticatedUser，包含userid、username、departments、ttl，不持有凭据；部门在身份对象中为不可变元组，对外JSON为数组。 |
| app/bootstrap.py | 装配并在关闭平台会话前释放用户认证 HTTP 客户端；删除本地 YAML 与免登码装配。 |

身份查询使用现有 MIDDLEWARE_BASE_URL、MIDDLEWARE_PLATFORM_CODE、MIDDLEWARE_SECRET 和 MIDDLEWARE_REQUEST_TIMEOUT_SECONDS；没有新增本地用户缓存 TTL。平台连接失败仍不阻塞服务启动，但所有受保护请求返回503，不能绕过认证。

---

## 稳定 ID 与名称快照

- 会话所有者、驻留工作区、记忆、附件、运行轮次及提取任务使用 userid。用户重新登录、更换 token 或修改 username 不改变所有权；不同 userid 即使同名也不能互访。
- conversations.owner_userid 保存稳定 ID；应用内部 owner_userid 参数为普通非空字符串，不再传密钥容器。
- 入库快照新增 submitted_by_userid，submitted_by 保留提交时用户名；批准入库后分别落为 contracts.uploader_userid 和 uploader。
- 删除审核快照增加 requested_by_userid 和 uploader_userid；列表/详情按两者 OR 限制人员范围，不再按姓名匹配。名称继续用于展示和中间件业务人员字段。
- 部门名称按中间件契约去首尾空白、非空且唯一；可属于多个部门，顺序不表示主部门。仅透传用户资料，不参与管理员授权、userid所有权和A2A文件等级过滤，不落合同或任务快照，不注入模型上下文。
- 用户 token 仅在当次认证请求中使用，不写入数据库、任务快照、工具上下文或应用日志。

---

## 历史数据迁移

初始化增量增加正式合同与删除审核的 userid 列，旧记录保持 NULL；旧入库快照允许缺失 submitted_by_userid。不按姓名或文件上传人名称自动认领旧数据。

旧会话 secret_key 无法可靠映射中间件 userid：迁移时保留会话及全部轨迹，添加 owner_userid=NULL，保存 legacy_owner_fingerprint（原密钥的 SHA-256）供后续受控映射核对，然后移除明文密钥列。未映射记录不会出现在新用户的会话列表中，也不能通过已知会话 ID 读取；重复初始化不会把指纹当作 userid 或重新分配归属。升级前应一致备份 SQLite，确需恢复旧所有权时必须另行提供可信映射，不能以同名用户推断。

旧删除审核没有可靠 userid 时也不通过名称开放给新身份；审核快照仍保留，后台消费、正式删除及清理机制继续工作。旧已提交提取申请只有在拥有稳定提交人 ID 时才能被对应用户幂等读取，未完成的内存提取运行随重启释放。

本轮不迁移用户数据到中间件，不改变合同通行证、审核审批语义或文件级别；这些由各自契约负责。

---

## 部署与验证

主分支移除 app/user 本地目录加载、POST /auth/login、LoginCodeCache、REVIEWER_USER_FILE、AUTH_LOGIN_CODE_TTL_SECONDS 和 deploy 的 user.yaml 挂载。无需准备本地用户文件；旧命名卷中残留用户文件不被应用读取，也不据其授权。独立应用 local-deploy 分支仍遵循自身契约，不修改该存档分支。

测试使用模拟中间件 HTTP 与临时 SQLite，验证 Bearer 分离、登录有效性、管理员即时撤销、错误分类、异常结构拒绝、同请求只验证一次、所有业务路由保护、token 更换与同名隔离，以及旧数据迁移保留。未改写真实中间件用户、管理员或登录状态。

部门契约回归见 `tests/test_user_departments.py`，使用MockTransport与真实/auth/me路由验证中间件解析、部门透传/即时更新、旧响应兼容、异常响应503、失效登录和管理员权限边界，不修改真实中间件用户资料。
