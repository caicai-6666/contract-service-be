# Communication 前后端样式联调服务

> **仅供开发：** 展示工作流使用确定性模拟文案，不执行真实门禁、合同分析或入库。可通过开关接入现有后端，也可启动独立服务；默认配置关闭模拟逻辑。

---

## 用途与启动

推荐直接使用现有后端，在项目 `.env` 设置：

```dotenv
COMMUNICATION_DEMO_ENABLED=true
```

重启原有 `app/main.py` 服务后，继续通过 **20000** 端口登录、创建轮次、订阅 SSE、取消和读取快照，不需要更改前端代理或单独启动脚本。当前本机已开启此配置，`.env.example` 保持 `false`。服务重启会清空原登录态和内存轮次，需要重新登录并新建轮次。联调结束后设为 `false` 并重启；部署环境不要开启。

现有后端启动仍正常装配 ES、合同目录及其他业务能力；仅 communication 的事件生产者替换为 `app/service/communication_demo.py` 中的展示流程。首次订阅自动执行，未指定场景时默认正常完成。其他合同接口不受影响。

### 可选：独立服务

脚本 [`scripts/serve_communication_demo.py`](../../../scripts/serve_communication_demo.py) 启动独立 FastAPI 服务，复用正式登录、communication 路由、事件 Schema 和内存运行时。接口契约见[多轮对话 API](../../api/communication.md)，状态及回放机制见[Communication 事件运行时](../../architecture/system/communication-events.md)。

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
| `[demo:clarify]` | 第一个文件剔除、最终确认问题 | `completed` |
| `[demo:rejected]` | 问题不相关及最终提示 | `rejected` |
| `[demo:failure]` | 半段文本、不可恢复错误、中断消息 | `failed` |
| `[demo:recoverable]` | 可恢复错误、恢复进度、最终总结 | `completed` |
| `[demo:slow]` | 长任务进度，便于点击取消或补充 | `completed`，除非提前终止 |
| `[demo:heartbeat]` | 阶段输出后空闲，观察 SSE 心跳 | `completed` |
| `[demo:replay]` | 生成超过缓存长度的进度，验证旧游标恢复 | `completed` |

正常流程依次展示处理计划、合同检索、条款读取、付款计算、条款对比和最终总结，共 6 条阶段消息及 1 条最终消息。固定样本包含两名虚构负责人、120 万元比较基数、付款比例与金额表，便于测试长文本、Markdown 表格及连续结果展示。

每个模拟工具通过现有 `task.progress.message` 展示用户可理解的动作、业务条件和完成结果，例如“正在检索合同 -- 关键词：设备采购｜类别：采购合同｜候选数：2”，随后以 `intermediate` 流式输出中间结果。函数名与调用编号仅记录在服务日志中，不拼入用户文案。没有新增结构化 `tool.*` 事件，前端直接展示文本，不应解析文案推断工具状态。阶段说明是公开处理摘要，不是模型内部推理。刷新时以快照恢复阶段消息及最新进度，不声称保留全部历史工具状态。

默认每个工具等待 4 秒（两段各 2 秒），加上每 4 字符间隔 0.2 秒的流式输出，完整正常流程约一分钟以上；实际时长受文案长度与调度影响，不伪造固定耗时或百分比。`slow` 另加 60 秒等待。等待使用可取消异步协程，不阻塞其他请求，取消或替换仍立即结束旧流程。

PDF 上传仍走正式的数量、大小及文件名校验，但不解析内容。所有场景均不发送门禁专属事件或卡片，校验动作使用通用进度，结论使用普通消息。`clarify` 将第一个实际上传文件描述为无法读取并剔除，其余文件列为保留，逐项说明名称及结果，再以最终消息请求确认或重新上传；无文件则只提示上传，不生成虚构文件列表。`rejected` 通过最终消息提示问题不相关，并进入拒绝终态。检查结果消息同步进入历史并随终态备份，刷新后仍可查看。本脚本不模拟可点击的真实合同引用，也不执行真实 PDF 校验。

为减少界面噪声，消息不再重复添加 `[联调模拟]` 前缀，也不发送“4/4 步骤已完成，正在汇总最终答复”提示；保留实际用于联调的工具进度、阶段结果和最终答复。模拟模式由启动配置、服务日志和本文档说明，数据仍是固定虚构样本，不是真实分析结论。

---

## 联调步骤与边界

1. 登录后创建轮次，检查 `pending_activation`；此时没有模拟任务，也没有输出事件。
2. 拿到 `turn_id` 后调用 `stream_turn_events`：首次有效订阅激活，开始模拟输出。重复订阅或断线重连不会重复执行场景。
3. 使用 `slow` 场景，在运行中调用真实取消接口，验证 `cancelled` 和 `context_status: user_manually_stopped`；或创建带 `supersedes_turn_id` 的新轮次，验证旧流 `superseded` 和 `context_status: user_goal_adjusted`。替换会停止旧模拟任务，新轮次仍须订阅才执行。
4. 确认问题以 `message_kind: final` 输出并结束这一轮。用户回复时另建轮次；脚本不理解回复，也不继承真实上下文，可显式选择下一轮场景。
5. 测试过期可用 `--activation-timeout 5` 启动，创建后不订阅，超过 5 秒再订阅应返回 `410`；快照状态为 `expired`。
6. 测试回放可用 `--event-buffer-size 16` 配合 `replay` 场景，待大量事件产生后以 `Last-Event-ID: 1` 重连，应返回 `409`。获取快照后按 `last_sequence` 恢复；正在连接的慢客户端也可能收到不带 ID 的恢复错误，处理方式见 API 文档。

取消与替换的状态来自正式运行时，脚本额外停止模拟生产协程；断开 SSE 不取消执行。确认场景若仅上传一个文件，该文件会被剔除，列表中不会虚构另一个真实可用文件。

服务使用单进程内存，重启清空免登码、轮次和输入，不保存对话历史。默认保留正式服务的容量与 TTL 限制，终态轮次仍占用保留名额；密集联调遇到容量限制时可重启模拟服务。不能把在正式服务创建的 `turn_id` 拿到模拟服务订阅，反之亦然。

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

模拟实现位于 `app/service/communication_demo.py` 的 `DemoEventService`，仅替换事件生产者；后端装配层通过配置选择它，独立脚本也复用同一实现。上表命令行参数只用于独立脚本；现有后端采用默认场景参数，同样支持文字场景标记。场景测试覆盖全部八种输出、交错事件、一次性激活以及取消、替换后的协程清理；现有 communication 测试继续负责接口及生命周期通用契约。

```bash
python -B -m unittest discover -s tests -p 'test_communication*.py' -q
```

本地 `tests/` 遵循项目现有忽略规则，不随 Git 提交。
