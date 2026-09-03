# 审核用户 YAML 定义

> **用途：** 本文定义本机审核用户的名称、密钥文件结构，以及应用启动时形成的内存对象边界。

---

## 文件位置与结构

默认文件为 `data/user/users.yaml`，可通过 `REVIEWER_USER_FILE` 指定其他路径；相对路径统一按项目根目录解析。文件只包含非空的 `users` 列表：

```yaml
users:
  - name: "默认审核人"
    secret_key: "change-this-local-reviewer-key"
```

每个用户必须包含且只包含以下字段：

| 字段 | 约束 | 用途 |
| --- | --- | --- |
| `name` | 去除首尾空白后非空，最长 100 字符，文件内唯一 | 审核人显示名称与查找键。 |
| `secret_key` | 非空、不能只包含空白且文件内唯一 | 审核人对应密钥，并支持登录时仅凭密钥反查用户。 |

文件顶层出现额外字段、用户条目出现额外字段、列表为空、名称重复、密钥重复或字段类型错误均视为配置错误。

---

## 启动与内存对象

`app.user.load_reviewer_user_catalog` 在 FastAPI 生命周期开始时读取一次 YAML，把每个条目转换为不可变 `ReviewerUser`，并构造带源文件路径和 SHA-256 内容指纹的 `ReviewerUserCatalog`。快照保存于：

```python
application.state.reviewer_user_catalog
```

可通过 `catalog.get(name)` 按名称取得用户对象，通过 `catalog.authenticate(name, secret_key)` 校验名称与密钥，也可通过 `catalog.find_by_secret_key(secret_key)` 为登录接口反查唯一用户。密钥字段使用 Pydantic `SecretStr`，对象表示和普通日志只显示掩码；确需使用明文时必须显式调用 `user.secret_key.get_secret_value()`。

登录接口会把审核用户转换为限时免登码；除健康检查和登录外的接口统一通过 FastAPI 依赖校验免登码并取得审核人名称。合同任务在创建时以该名称记录所有者，后续运行列表、快照、SSE、继续和重试都按所有者隔离；客户端不能自行声明任务所有者。当前尚未实现角色权限。接口契约见[审核用户登录 API](../../api/auth.md)和[合同 API](../../api/contract.md)。

---

## 安全与运维

- 仓库中的默认密钥只适用于本机开发，部署前必须替换。
- 应限制 YAML 文件的操作系统读取权限，不得把完整用户对象或密钥明文写入日志。
- 修改 YAML 后需要重启应用；运行期间不会重新扫描文件或原地修改快照。
- 任一加载或 Schema 错误都会阻止应用启动，避免服务在缺少审核用户的情况下继续运行。
