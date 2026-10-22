# 文件等级调整消费与历史

消费注册中心通过中间件下发的文件等级调整；`source_id` 对应本平台合同 `document_id`。先在同一 SQLite 事务更新当前等级并记录历史，提交成功后才 ack。依赖[平台会话](../infrastructure/middleware-session.md)及[合同元数据存储](../../architecture/data/contract-sqlite-metadata.md)，不修改用户身份或平台管理员资格。

---

## 消费协议与生命周期

| 接口 | 请求与响应处理 |
| --- | --- |
| `POST /middleware-service/api/file-permission-adjustments/pull` | 平台 Bearer，无请求体；200 校验 offset 和完整 message，204 结束本轮并继续定时轮询。 |
| `POST /middleware-service/api/file-permission-adjustments/ack` | 平台 Bearer，JSON 仅含 message_id；核验 acknowledged、同一消息 ID 和当前 pull 的 offset。 |

消息包含 `message_id`、`target_platform_code`、`source_id`、`file_level`、`reviewer`、`note`、`created_at`。本平台 source_id 必须为完整 64 位小写合同哈希，目标平台必须等于本平台代号；等级为严格整数，不限制范围，拒绝布尔、字符串、小数和 null。操作人及消息标识不得为空白，备注允许空字符串，时间必须带时区。消息没有 passport 或文件附件。

`MiddlewareClient` 封装 HTTP，`FilePermissionAdjustmentConsumer` 提供 `start()`、`scan_once()`、`close()`；`app.bootstrap` 在正式存储就绪后启动，并在关闭依赖前停止。扫描互斥、消息顺序处理，远端不可用不阻止应用启动。无有效平台令牌时等待；401 仅使本次平台令牌失效，随后由平台会话服务重登。

---

## 事务、跳过与确认

1. 按 message_id 检查历史。已处理且内容一致则复用结果，不重复修改；同 ID 内容变化时失败，不 ack。
2. 首次处理在 SQLite 写事务内读取合同：ready 合同更新当前等级并插入 applied 历史；目标不存在则只插入 skipped_missing 历史。仍在入库或实际删除的合同等待下轮重试，不 ack。
3. 事务提交后重新读取有效令牌，再调用 ack；确认成功后幂等保存本地 acknowledged_at。

相同等级消息也记录处理凭据。缺失文件按约定跳过并确认，之后即使该文件出现，旧消息重投仍不补执行。当前等级与历史插入失败一起回滚，远端消息保留供下次处理。

ack 失败、超时、响应丢失或进程重启后，下一轮重新 pull，通过已提交历史重试确认，不会把文件回退到旧等级。同消息重投到不同 offset 时核验本次交付位置。ack 本地时间写入失败也不重复修改等级；该时间只表示本地收到过成功响应，不是远端消费进度的权威查询。

正式入库回执重试通过当前入库实例第一条调整的 previous_level 核对初始审核等级，保留后续已调整的等级。

---

## 历史与存储方法

历史表字段见[合同元数据结构](../../architecture/data/contract-sqlite-metadata.md#文件等级变更历史)。与正式元数据共用 `data/abstract/contracts.db`，不随合同删除或审核临时区清理而删除。初始化只建表，不为已有等级伪造历史。

| SQLiteContractMetadataStore 方法 | 行为 |
| --- | --- |
| `apply_file_permission_adjustment(delivery, platform_code=...)` | 同事务更新当前等级与历史，或返回已处理结果。 |
| `acknowledge_file_permission_adjustment(message_id)` | 远端 ack 成功后保存本地确认时间。 |
| `list_file_level_history(document_id=None)` | 默认按历史序号倒序读取全部记录；内部调用可传文档 ID，已删除合同也可查。 |

前端通过[全部文件等级变更记录接口](../../api/contract.md#获取全部文件等级变更记录)读取已消费落库的记录，无需传合同 ID，不分页，不重新拉取或确认消息。没有新增直接修改等级的 HTTP 接口。正式元数据接口展示更新后的等级，入库及删除审核历史保留各自原有快照。

---

## 配置、限制与验证

| 环境变量 | 默认值 | 约束 |
| --- | --- | --- |
| `FILE_PERMISSION_ADJUSTMENT_POLL_INTERVAL_SECONDS` | `30` | 每轮完成或失败后的等待秒数，有限正数。 |
| `FILE_PERMISSION_ADJUSTMENT_BATCH_SIZE` | `10` | 每轮消息上限，正整数。 |
| `MIDDLEWARE_REQUEST_TIMEOUT_SECONDS` | `10` | 复用平台 HTTP 请求超时。 |

后端与 deploy 的 `.env.example` 均提供配置；无需本平台连接 RabbitMQ。

上游消息没有原入库实例时间或版本。尚未处理的旧消息若在同哈希文件重新入库后才首次交付，现有契约无法识别是否属于上一实例，当前按收到时的正式合同处理。已处理消息重投则由历史幂等保护，不修改后来实例。

测试使用临时 SQLite 和模拟 HTTP，覆盖请求协议、严格类型、事务回滚、缺失目标跳过、内容冲突、重复/并发扫描、ack 失败重启恢复、旧消息不回退新等级及原入库重试。未拉取或确认真实消息。
