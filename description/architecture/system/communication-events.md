# Communication 事件运行时

> **实现范围：** 输入暂存、轮次生命周期、SSE、快照与历史同步；正式执行器已在订阅激活后调用完整门禁，完整门禁放行后已串接 Agent Core，复用同轮 SSE 与驻留历史。

应用装配统一使用 `CommunicationWorkflowService`，完整业务门禁通过后调用正式 Agent Core。演示服务、模拟问答分支及其配置开关已移除。

总体边界见 [Communication 总体设计](contract-communication.md)，对外协议唯一来源为[多轮对话 API](../../api/communication.md)。

---

## 职责与依赖

| 模块 | 职责 |
| --- | --- |
| `app/schema/communication.py` | 五类事件负载、消息快照、轮次快照的不可变模型。 |
| `app/service/communication.py` | 内部事件源注册、发布校验、日志缓存、快照投影及订阅。 |
| `app/router/communication.py` | 当前用户注入、HTTP 错误映射、SSE 编码及断连释放。 |
| `app/bootstrap.py` | 每个应用生命周期创建并关闭事件服务。 |

事件层复用 FastAPI、Pydantic 和 Python 异步能力，表单解析新增 `python-multipart`，无新增环境变量。服务不解析 PDF、不依赖模型，不直接把原始模型输出当作正式事件。应用启动时 bind_history 接入[统一驻留轨迹](communication-history.md)，注册需传入服务端认证取得的 secret_key；事件提交同步更新公开轨迹，SQLite 备份由历史服务负责。

业务门禁保留独立子图，但不占用专属事件、任务阶段状态或快照字段。执行层通过通用进度报告校验动作，通过普通消息报告结论；内部文件筛选数据不能从展示文案反推。对外协议与前端迁移要求见 [SSE 事件契约](../../api/communication.md#sse-事件契约)。

`task.progress` 的展示类型限定为 `local-search/online-search/external-expert/thinking`，与轮次生命周期独立。快照只保留最新的类型和业务说明，前端覆盖显示；执行器按业务阶段发进度，多个工具可以共用一个状态。工具调用和结果继续独立写入有序轨迹，不因对外减少播报而丢弃；进度本身不转成历史消息。

---

## 内部发布方式

后续业务服务在确认用户和轮次身份后，注册事件源并逐次发布公开事件。HTTP 不暴露任意事件发布接口。

```python
from app.schema.communication import (
    MessageDeltaData, MessageCompletedData, TaskProgressData, TurnStatusData,
)

# service 来自 application.state.communication_event_service。
await service.register_turn("c1", "t1", owner="alice", secret_key=authenticated_user.secret_key)
# 生产中由前端调用 GET events 触发；示例显式订阅激活后才能发布。
stream = await service.subscribe("c1", "t1", owner="alice")
await service.publish("c1", "t1", owner="alice",
                      data=MessageDeltaData(message_id="m1", message_kind="intermediate", delta="已完成检查。"))
await service.publish("c1", "t1", owner="alice",
                      data=TaskProgressData(type="thinking", message="正在准备分析"))
await service.publish("c1", "t1", owner="alice",
                      data=MessageCompletedData(message_id="m1", message_kind="intermediate", text="已完成检查。"))
await service.publish("c1", "t1", owner="alice",
                      data=MessageCompletedData(message_id="m2", message_kind="final", text="是否使用通过检查的文件继续？"))
await service.publish("c1", "t1", owner="alice",
                      data=TurnStatusData(status="completed"))
await stream.aclose()
```

注册时仅生成 `pending_activation` 快照，事件序号为 0，不执行工作流。首次有效订阅在同一锁内检查身份、期限和游标，再生成唯一的 `processing` 激活事件。`CommunicationWorkflowService` 为每轮启动唯一后台生产者，调用业务门禁子图；重复订阅、快照读取和重连不重跑任务。

激活和终态提交统一生成事件及快照的 `activated_at`、`finished_at`、`processing_duration_ms`。UTC 时间用于展示，私有单调时钟用于计算终态固定耗时；未激活结束耗时为零。计时在全部事件校验成功后提交，拒绝的事件不会改变计时，幂等取消不重新计时。字段与前端展示规则以[多轮对话 API](../../api/communication.md#展示快照)为准。

内部 `CommunicationEvent.created_at` 继续保留，但 SSE 编码只在 `turn.status` 中公开它。进度、消息和错误事件不携带时间，排序和回放依靠事件序号；执行器统一使用此编码规则，详见 [SSE 事件契约](../../api/communication.md#sse-事件契约)。

中断任务时，在临时副本上先收束活跃消息为 `message.completed(status=interrupted)`，再生成轮次终态；整批校验及历史投影成功才更新权威状态和唤醒订阅者。正常消息为 `status=completed`，生产者不得伪造中断消息。每条业务事件（delta 除外）使用同一公开编码写入完整展示记录，不受短期回放队列淘汰影响。

`publish` 在待激活状态下拒收业务事件；激活后校验 Schema、消息顺序、终态与容量再提交，失败不消耗序号或污染快照。注册、激活、过期和替代状态由服务生命周期管理，不能通过普通 `publish` 注入。发布者负责内容业务正确性与脱敏。

同一会话在当前缓存内不允许绑定多个用户；订阅、快照与发布均检查轮次所有者。这里的临时注册表不替代未来持久化会话权限模型。

同一会话最多有一个非终态事件源。若用户在执行中调整要求，内部调用 `register_turn(conversation_id, new_turn_id, owner=owner, supersedes_turn_id=old_turn_id)`。旧轮次必须属于同一会话和用户、仍未结束，新轮次标识必须不同且尚未使用。

服务先验证旧轮次中断权限、新轮次、容量和新旧事件负载，再在同一锁内提交：旧轮次发布 `superseded` 并关联新轮次，新轮次进入 `pending_activation` 并保留旧轮次关联。旧输入释放，旧消息若仍生成则标为 `interrupted`，旧订阅收到终态后关闭；新订阅激活后使用独立序号。正式工作流待激活及门禁中的旧轮次不可被替代，新轮次未及时激活不会恢复旧轮次。

容量或校验失败时旧轮次不变；并发替代同一个旧轮次时只有一个请求成功。旧轮次已结束时必须注册普通新轮次，不覆盖原终态。缓存仍保留旧记录用于回放，因此替代也需要一个新的容量名额。

---

## 缓存与并发

每轮使用有界事件队列和不可变快照。发布在条件锁内原子更新状态与日志后唤醒订阅者；订阅者使用序号读取，不为每个连接复制无界队列。慢客户端落后于保留范围时显式要求快照恢复，不静默跳过事件。

生成器断开只结束当前订阅。进度与文字可交错，但同轮只有一条活跃消息；实际多个生产者调用由锁串行提交，违反消息约束的调用显式失败。终态阻止迟到事件继续写入，但不负责取消外部计算或回滚工具副作用。

消息用途显式区分 `intermediate/final`，同一消息类型不可改变。正常完成必须已交付一条 `final`；最终答复完成后只接受轮次终态，不再发布新消息或进度。生产者可调用 `finish_with_message`，显式指定最终正文与 `completed/rejected/failed` 终态：先在副本验证两个事件，再一次投影历史并提交运行时，避免完整拒绝提示与任务状态半提交。既有逐条 `publish` 仍可用，事件层不推断模型何时结束。

澄清或确认请求也是本轮 `final`，然后 `completed` 关闭流。后续用户回复需注册同会话下的新轮次，不在旧轮次中等待或恢复；会话创建已通过 `POST /communication/conversations` 接入 SQLite，模型上下文继承仍未接通。异常取消、拒绝或失败可以没有最终答复。

快照保留用于展示的中断消息，不是权威模型工作区或私有审计。用户取消后的模型上下文处理见[用户消息与上下文设计](../workflow/contract-communication/user-context.md)，尚未实现。

`cancel_turn` 在同一条件锁内检查归属、当前状态及 `can_interrupt` 后提交 `cancelled`，随后唤醒订阅者。正式流程只有完整门禁通过并进入后续执行时才开放中断；注册、待激活及门禁期间禁止取消或替代。权限开放追加一次 processing 状态事件，激活时间及单调计时起点不变，历史同步更新。重复取消不重复发布，其他终态不改写。提交终态时权限归零、释放输入、标记中断消息；取消与替代、完成竞争的失败方不能改变既有终态。精确 HTTP 语义见 API 文档。

状态事件与快照通过同一模型基类派生 `context_status`，将替代映射为用户目标调整、取消映射为用户手动终止。映射规则见[用户消息与上下文设计](../workflow/contract-communication/user-context.md)。只读派生字段不进入发布输入校验，不维护第二份可变状态；模型上下文尚未消费此标记。

基础事件层保证终态后的写入隔离及中断权限的原子校验；正式 `CommunicationWorkflowService` 在允许的用户取消、替代或系统删除、TTL、关闭时停止后台执行。用户操作不能中断门禁，但系统清理可以。断开单个 SSE 连接不取消执行。同步 PDF 线程已经开始的操作不保证立即停止，但其结果不能再写入已终止轮次；远端模型是否立即停止计算仍取决于服务端。

正式门禁始终以 `thinking/正在思考` 展示，所有已知未放行结果进入统一拒绝节点，由模型依据精简业务日志生成回复；完整校验后按片段输出并收尾为 `rejected`，回复故障使用兜底文案。原始检查 failed 原因内部保留，不误称文件不合格；程序异常、生命周期失败和第二层故障仍可使用 `failed`。整个过程复用现有事件历史投影和终态后台备份，不新增独立拒绝记录或数据库列。只有完整门禁通过才允许进入后续执行；正式装配默认提供 Agent Core；仅独立测试未配置后续执行器时保留开发占位提示。详细前端识别、格式与恢复契约见 [门禁拒绝与恢复](../../api/communication.md#门禁拒绝与恢复)。

`tests/test_communication_workflow.py` 使用临时 SQLite 和内存 PDF，覆盖打开、渲染、视觉判断熔断，SSE/快照/驻留/落库恢复一致，取消半成品、方向调整、断线与删除、重复订阅和收尾原子性。视觉响应通过桩替代，测试不请求真实模型，也不写生产附件。

---

## 生命周期与限制

当前构造参数默认值如下；如需调整，由应用装配时显式传入，不擅自复用合同提取配置。

| 参数 | 默认值 |
| --- | --- |
| 每轮回放事件数 `event_buffer_size` | 128 |
| 心跳间隔 `heartbeat_seconds` | 15 秒 |
| 首次激活期限 `activation_timeout_seconds` | 180 秒 |
| 回收检查间隔 `cleanup_interval_seconds` | 1 秒 |
| 暂存输入总量 `max_staged_bytes` | 64 MiB |
| 最近一次成功发布后的保留时间 `ttl_seconds` | 3600 秒 |
| 同时保留轮次数 `max_turns` | 32 |
| 单事件内部 JSON UTF-8 大小 `max_event_bytes` | 32768 字节 |
| 每轮消息文本总字符数 `max_message_chars` | 65536 |

每轮最多 128 条消息。容量满时注册失败，不静默驱逐未过期轮次；单事件或消息上限触发时拒绝发布，业务生产者需处理这些异常。正常 `message.completed` 仍受单事件上限约束；唯一例外是运行时自动生成的 interrupted 收束事件，允许超过单事件额度，但其正文已受轮次总字符数限制，避免长半成品导致取消、替代或关闭失败。完整展示记录随历史保留，不受 SSE 缓存条数限制，总历史容量仍需后续治理。

启动时运行后台回收，默认每秒检查；请求路径也检查截止时间。首次激活使用单调时钟判断，响应同时提供 UTC 截止时间；到期立即拒绝订阅，后台最长一个检查周期释放暂存资源。未激活轮次不受普通日志 TTL 提前删除。

过期进入 `expired`，清除输入，仅保留有界事件与快照用于识别 `410`；过期记录之后按普通日志 TTL 清理为 `404`。这些保留记录仍占用轮次数容量。读取和心跳不续期，激活与成功发布刷新日志保留时间；激活后的执行和重连不受原 180 秒期限限制。

文字及上传 PDF 字节暂存在私有 `TurnInput`，不进入公开 SSE 或快照，供后续执行层通过 `get_input` 读取。HTTP 先做空值、名称和大小检查再注册，不进行文件内容门禁；完成、失败、取消、替代、过期或服务关闭均释放服务持有的输入。当前不继承旧轮次输入，原文字、附件引用、内部轨迹及精简展示事件由统一历史服务保留并备份。

上传最多 10 份、每份 10 MiB、总量 20 MiB，文字最多 20000 字符。框架解析 multipart 时可能先写临时文件，随后应用分块读取到内存并关闭上传句柄；应用容量限制不覆盖整个 HTTP 解析阶段，生产代理仍应配置请求体大小和并发限制。

应用关闭清空日志并唤醒等待者；进程重启后记录丢失。当前必须与已有单 worker 部署一致，不支持跨进程事件共享或持久化恢复，也不提供完整历史审计。

---

## 验证

本地测试 `tests/test_communication_events.py` 覆盖交错输出、顺序消息、最终答复约束、澄清后结束及新轮次隔离、类型一致性、终态与迟到结果、用户隔离、心跳、重连与慢客户端、游标缺口、TTL、容量、SSE 文本转义、HTTP 与 OpenAPI。测试目录按项目现有规则不纳入 Git。

这些检查使用内部注册和测试事件，不证明实际 PDF 门禁、模型流式响应、取消或生产代理链路已经接通。


## 流式预览失败的收束

Agent Core 的两种用户输出工具支持生成中的正文预览。预览截断或校验失败时，内部 `interrupt_output` 在会话锁内读取指定活动消息并提交 interrupted，保留当前任务以继续纠错；普通 publish 仍不允许调用方自行提交 interrupted。已终止任务或非活动消息不会被改写，正文取自服务端快照。此过程与取消任务不同，不额外产生 turn.status 终态。详见[交互工具增量展示](../workflow/contract-communication/interaction-tools.md#增量展示)。
