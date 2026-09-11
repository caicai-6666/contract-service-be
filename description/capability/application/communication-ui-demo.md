# Communication 前后端样式联调服务

> **仅供开发：** 现有后端采用“真实门禁 → 通过后模拟问答”的混合模式；不执行真实合同检索、分析或入库。独立脚本保留不依赖模型的纯模拟模式。默认配置关闭模拟问答。

---

## 用途与启动

推荐直接使用现有后端，在项目 `.env` 设置：

```dotenv
COMMUNICATION_DEMO_ENABLED=true
```

重启原有 `app/main.py` 服务后，继续通过 **20000** 端口登录、创建轮次、订阅 SSE、取消和读取快照，不需要更改前端代理或单独启动脚本。当前本机已开启此配置，`.env.example` 保持 `false`。服务重启会清空原登录态和内存轮次，需要重新登录并新建轮次。联调结束后设为 `false` 并重启；部署环境不要开启。

现有后端始终装配 `CommunicationWorkflowService`。首次订阅先运行真实文件可读性、文件摘要和四维相关性聚合；只有 passed 才调用 `run_demo_workflow(after_gate=True)` 输出模拟问答。开关 false 时同样执行真实门禁，通过后保留核心问答未接入提示。其他合同接口不受影响。

门禁拒绝或检查执行失败时均进入[统一拒绝与响应](../../architecture/workflow/contract-communication/business-gate.md#统一拒绝与响应)，生成真实情况的友好提示并以 rejected 结束，不启动模拟工具或消息。门禁和 mock 共用一个 turn_id、生产协程、SSE 队列、计时和历史记录；门禁通过时不先发送 completed 或最终消息，不创建第二个流。回复模型不可用时使用兜底文案，不降级为 mock 放行；第二层执行故障仍按 failed 处理。

### 可选：独立服务

脚本 [`scripts/serve_communication_demo.py`](../../../scripts/serve_communication_demo.py) 启动不执行真实门禁的独立 FastAPI 服务，复用正式登录、communication 路由、事件 Schema 和内存运行时。接口契约见[多轮对话 API](../../api/communication.md)，状态及回放机制见[Communication 事件运行时](../../architecture/system/communication-events.md)。

首次请求先调用 `POST /communication/conversations` 创建会话和首轮，不能再自行编造会话 ID 调用 create_turn。正式服务持久化会话及工作区；独立脚本使用临时 SQLite 会话库，关闭时清理，不将联调密钥写入正式数据库。两种模式均实时收集统一轨迹并后台备份终态；SSE 事件仍在内存。正式服务已备份轨迹跨重启保存；独立脚本退出时会清理临时数据库及 upload，不能用于验证跨重启持久化。

在项目根目录、已安装 `requirements.txt` 的 Python 环境中运行：

```bash
python scripts/serve_communication_demo.py --port 20001
```

前端将 communication 和登录请求指向 `http://127.0.0.1:20001/contract/api`。若浏览器跨域直连，显式允许实际前端 Origin，例如：

```bash
python scripts/serve_communication_demo.py --port 20001 --allow-origin http://localhost:5173
```

默认读取现有配置的审核用户 YAML，也可通过 `--user-file /path/to/user.yaml` 指定；配置只在启动时加载。必须向模拟服务重新登录，正式服务的免登码不能跨进程使用。默认只监听本机；确需局域网访问时显式传入 `--host 0.0.0.0`，并自行限制网络访问范围。

服务只提供登录、communication、健康检查和 FastAPI 文档，不提供合同目录或 PDF 资源接口。健康检查 `/contract/api/health` 返回 `mode: communication-demo`，便于确认没有误连正式后端。

---

## 选择场景

通过原有 `create_turn` 表单的 `text` 字段提交场景标记，例如 `[demo:success] 请分析这份合同`；无需新增接口参数。标记须位于文字开头（允许前导空白），使用下表小写值。不带标记时使用启动参数 `--scenario`，默认为 `success`。未知小写场景标记以 `demo_driver_error` 和 `failed` 结束。

| 标记 | 展示内容 | 最终状态 |
| --- | --- | --- |
| `[demo:success]` | 通用校验进度、阶段文本与进度交错、Markdown 总结 | `completed` |
| `[demo:online]` | 正常流程加联网检索展示，覆盖全部三种进度类型；不发起真实网络请求 | `completed` |
| `[demo:clarify]` | 第一个文件剔除、最终确认问题 | `completed` |
| `[demo:rejected]` | 问题不相关及最终提示 | `rejected` |
| `[demo:failure]` | 半段文本、不可恢复错误、中断消息 | `failed` |
| `[demo:recoverable]` | 可恢复错误、恢复进度、最终总结 | `completed` |
| `[demo:slow]` | 长任务进度，便于点击取消或补充 | `completed`，除非提前终止 |
| `[demo:heartbeat]` | 阶段输出后空闲，观察 SSE 心跳 | `completed` |
| `[demo:replay]` | 生成超过缓存长度的进度，验证旧游标恢复 | `completed` |

上表的拒绝和文件剔除语义仅适用于独立纯模拟脚本。混合模式下，场景标记作为原始文字参与真实门禁，不能强制放行或拒绝；门禁通过后 `[demo:rejected]` 按 success 输出，`[demo:clarify]` 只询问希望优先核对的分析方向，不再伪造文件不可读或剔除真实准入附件。其他场景只控制通过后的模拟阶段。

正常流程只展示检索结果、关键差异两条阶段消息及一条最终总结；输入理解、条款读取、付款计算等操作使用进度状态，不再逐工具播报或输出完整处理计划。固定样本包含两名虚构负责人、120 万元比较基数、付款比例与金额差异，便于测试 Markdown 及流式结果展示。

`task.progress` 使用必填 `type` 和 `message`：查阅合同与条款共用 `local-search`，计算与对比共用 `thinking`，`online` 场景额外使用 `online-search`。类型及默认文案见 [SSE 事件契约](../../api/communication.md#sse-事件契约)。前端按类型渲染当前状态，用后续进度覆盖旧状态，不堆积工具卡片；进度、消息和错误事件不携带时间。四次本地模拟工具调用及结果仍完整保存在内部有序轨迹中，联网场景另有一次模拟检索记录，工具名和调用 ID 不拼入公开进度。刷新时以快照恢复关键消息及最新进度，终态停止动画。

重新打开会话时使用 `payload.events`，它保存与实时流一致的业务事件但排除 delta，不再返回内部工具 trace。`failure` 或主动取消、替代遇到半成品时，先输出并保存 `message.completed(status=interrupted)`，再记录任务终态；正常消息为 `status=completed`。活跃任务恢复还需应用 `streaming_messages` 并按 `last_sequence` 续接，详见[用户展示 Payload](../../api/communication.md#用户展示-payload)。

默认每个工具等待 4 秒，加上每 4 字符间隔 0.2 秒的流式输出，模拟阶段约半分钟到一分钟，混合模式还需加上真实门禁耗时；实际时长受文案长度与调度影响，不伪造固定耗时或百分比。`slow` 另加 60 秒等待，保持同一个思考状态，不重复发送步数。等待使用可取消异步协程，不阻塞其他请求，取消或替换仍立即结束旧流程。

独立纯模拟脚本的 PDF 上传只走数量、大小及文件名校验，不解析内容；混合模式会真实打开、渲染、检查视觉可读性并生成名称与摘要。以下伪造检查结果仅属于独立脚本。所有场景均不发送门禁专属事件或卡片，校验动作使用通用进度，结论使用普通消息。`clarify` 将第一个实际上传文件描述为无法读取并剔除，其余文件列为保留，逐项说明名称及结果，再以最终消息请求确认或重新上传；无文件则只提示上传，不生成虚构文件列表。`rejected` 通过最终消息提示问题不相关，并进入拒绝终态。检查结果消息同步进入历史并随终态备份，刷新后仍可查看。独立脚本不执行真实 PDF 校验；两种模式均不伪造可点击的真实合同引用。

为减少界面噪声，消息不再重复添加 `[联调模拟]` 前缀，也不发送“4/4 步骤已完成，正在汇总最终答复”提示；保留实际用于联调的工具进度、阶段结果和最终答复。模拟模式由启动配置、服务日志和本文档说明，数据仍是固定虚构样本，不是真实分析结论。

---

## 联调步骤与边界

1. 登录后创建轮次，检查 `pending_activation`；此时没有模拟任务，也没有输出事件。
2. 拿到 `turn_id` 后调用 `stream_turn_events`：首次有效订阅激活；混合模式先执行真实门禁，通过后才开始模拟输出，独立脚本直接模拟。重复订阅或断线重连不会重复执行场景。
3. 使用 `slow` 场景，在运行中调用真实取消接口，验证 `cancelled` 和 `context_status: user_manually_stopped`；或创建带 `supersedes_turn_id` 的新轮次，验证旧流 `superseded` 和 `context_status: user_goal_adjusted`。替换会停止旧模拟任务，新轮次仍须订阅才执行。
4. 确认问题以 `message_kind: final` 输出并结束这一轮。用户回复时另建轮次；模拟问答不理解回复，可显式选择下一轮场景；混合模式下一轮的真实门禁仍按近期有效历史判断关联。固定虚构样本不能作为真实业务结论。
5. 测试过期可用 `--activation-timeout 5` 启动，创建后不订阅，超过 5 秒再订阅应返回 `410`；快照状态为 `expired`。
6. 测试回放可用 `--event-buffer-size 16` 配合 `replay` 场景，待大量事件产生后以 `Last-Event-ID: 1` 重连，应返回 `409`。获取快照后按 `last_sequence` 恢复；正在连接的慢客户端也可能收到不带 ID 的恢复错误，处理方式见 API 文档。

取消与替换的状态来自正式运行时，脚本额外停止模拟生产协程；断开 SSE 不取消执行。确认场景若仅上传一个文件，该文件会被剔除，列表中不会虚构另一个真实可用文件。

混合模式由正式执行器在完整门禁 passed 后显式批准本轮附件，保留真实 display_name/summary，并随终态轨迹后台备份至 upload；mock 不重复提交或改写准入。门禁拒绝、失败、未完成均不批准文件保存；门禁期间用户取消/替换返回 `409`，不能提前结束。完整门禁通过进入模拟阶段时以 `turn.status` 开放 `can_interrupt`，前端据此启用取消与方向调整；通过后的模拟分析失败或用户中断仍保留已准入附件。独立纯模拟脚本没有真实门禁，保留原有取消行为。

独立脚本继续模拟附件准入：rejected 剔除全部，clarify 剔除首份，其余场景模拟全部通过。两种模式均遵守[延迟落盘机制](../../architecture/system/communication-history.md#附件准入与延迟落盘)。关闭演示开关后真实门禁仍执行，但因没有后续问答执行器，继续提示未接入且不批准附件。

演示与正式门禁统一以 `thinking/正在思考` 显示校验状态。`rejected` 场景按[门禁拒绝与恢复](../../api/communication.md#门禁拒绝与恢复)中的标题、原因列表和下一步格式流式输出，并将完整最终消息与 rejected 终态原子提交，供前端同时验证实时展示和历史恢复。

服务使用单进程内存，重启清空免登码和实时轮次；混合模式已备份的历史与附件保留在正式 SQLite/upload，独立脚本退出会删除临时历史库和 upload。默认保留正式服务的容量与 TTL 限制，终态轮次仍占用保留名额；密集联调遇到容量限制时可重启模拟服务。不能把在正式服务创建的 `turn_id` 拿到模拟服务订阅，反之亦然。

---

## 配置与验证

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `--delay` | `0.2` 秒 | 每 4 个字符输出一块的间隔 |
| `--slow-seconds` | `60` 秒 | 长任务额外等待总时长，不含阶段文本输出 |
| `--tool-seconds` | `4` 秒 | 每个模拟工具的等待总时长，不含结果文字输出 |
| `--heartbeat` | `3` 秒 | SSE 空闲心跳间隔 |
| `--activation-timeout` | `180` 秒 | 首次订阅激活期限 |
| `--event-buffer-size` | `128` | 每轮保留事件数 |
| `--allow-origin` | 无 | 可重复指定跨域前端 Origin |

模拟实现位于 `app/service/communication_demo.py` 的 `run_demo_workflow`，只向调用者的同轮事件与轨迹队列输出。后端装配层将其注入 `CommunicationWorkflowService.after_gate`；独立脚本通过 `DemoEventService` 直接调用同一函数，保留独立生产者生命周期。上表命令行参数只用于独立脚本；现有后端采用默认场景参数，同样支持文字场景标记。`tests/test_communication_hybrid.py` 验证开关两态都装配真实门禁、同流串接、失败短路、真实摘要保留、附件备份、场景标记不能推翻准入，以及门禁/模拟阶段取消替换；测试模型使用桩。纯模拟场景测试覆盖全部九种输出、三类进度、交错事件、一次性激活以及取消、替换后的协程清理；现有 communication 测试继续负责接口及生命周期通用契约。

```bash
python -B -m unittest discover -s tests -p 'test_communication*.py' -q
```

本地 `tests/` 遵循项目现有忽略规则，不随 Git 提交。
