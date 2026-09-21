# Communication 会话轨迹与后台备份

## 职责与入口

任务条目顶层的 `can_interrupt` 与运行时快照共享锁更新，open/refresh 直接返回当前权限。终态始终为 false，SQLite 无需新增列；精简 SSE 历史保存权限变化，旧记录及摘要缺省为 false。完整规则见[门禁阶段的用户中断限制](../workflow/contract-communication/user-context.md#门禁阶段的用户中断限制)。

`ConversationHistoryService` 是会话历史与工作区的统一内存数据源。每个 `ResidentConversation` 同时持有有序轨迹、工作区快照及备份状态。`CommunicationEventService` 与其共用锁，在接受公开事件时同步更新轨迹；SSE 回放队列和展示快照只是传输、展示投影，不作为完整历史来源。

正式应用和独立展示脚本均通过 `bind_history` 装配。前端通过 [open 与 refresh](../../api/communication.md#打开会话) 获取当前驻留范围的全部任务，服务端过滤摘要；不需要等待 SQLite 备份。实时问答工作流及最终问答模型上下文组装尚未接入，后台记忆加工已由独立归档服务接入。

用户恢复不再直接消费内部 trace。运行时接受事件时同步记录 payload.events（排除 delta），与 trace、event_cursor 和任务状态同批提交；`communication_display.py` 只将输入、展示事件及活跃消息累积正文投影给前端，隐藏工具轨迹。旧任务只兼容聚合已有消息，不伪造遗漏的进度和错误。精确接口见[用户展示 Payload](../../api/communication.md#用户展示-payload)。

---

## 状态与顺序

| 内部状态 | 轨迹可更新 | 后台处理 |
| --- | --- | --- |
| 待激活或处理中、fresh | 可以，由运行时接受的操作更新 | 不备份 |
| 终态、fresh | 不可再修改 | 复制到 SQLite，失败重试 |
| 已持久化 | 不可再修改 | 无需重复备份 |

实现以记录终态和内部 `_persisted` 标记集区分上述状态；标记与归属密钥不进入 payload 或前端响应。加载的 SQLite 记录标记为已持久化。

注册轮次时先校验归属并加载最近窗口，再按会话内最大 sequence 分配新序号和 record_id。后续正常完成、取消、替换、拒绝、失败、激活超时均冻结原身份及顺序；后台不得重新编号。旧 SSE 轮次过期不会删除会话轨迹；处理中运行时 TTL 到期以 failed 冻结后回收事件源。

用于上下文截断的累计摘要生成尚未接入（不同于已实现的单任务检索总结）。未来摘要写入必须加入同一序号分配与锁协议，禁止绕开驻留层直接向存在 fresh 任务的会话追加摘要或任务，否则可能序号冲突；当前加载与备份遇冲突拒绝覆盖。

---

## 公开轨迹收集

`communication_trace.py` 只接收已校验的用户可见消息，以及执行层显式提交的 `record_tool_call / record_tool_result`。这些内部记录方法不是模型工具，也不是 HTTP 任意写入接口。Agent Core 已接入工具记录。

- 连续的同消息增量合并；工具穿插后保留新的片段，不跨工具合并。
- message.completed 仅确认片段状态与引用，不重复追加全文；无增量时可直接记录完整消息。
- 活动消息允许 streaming；终态时未完成消息与工具统一标为 interrupted，不伪造成功结果。
- task.progress 更新即时进度并原样保存到展示 events，不进入内部 trace 消息，也不从文案推测调用。
- 公开轨迹不保存模型私有推理、错误纠正链或权威工作区内容。
- 旧 SSE 正式合同引用的 SHA-256 document_id 转为既定 /{document_id}.pdf；不接受无法映射的标识，不编造位置。工具结果支持 type/location 引用。SSE 原字段未改动。

输入保留原文与文件列表。注册只分配 UUID 并在内存暂存附件；文件准入和落盘规则见下节。历史读取只返回元数据，不读文件字节。[附件资源接口](../../api/resource.md#读取已驻留会话任务的-pdf-附件)独立读取本人已驻留任务的准入文件；会话删除后的磁盘文件清理仍待实现。

---

## 附件准入与延迟落盘

`CommunicationEventService.resolve_file_admission(conversation_id, turn_id, owner=..., accepted_indices=(...))` 是供工作流使用的内部接口，不是 HTTP 接口或模型工具。下标从 0 开始，对应本轮原始上传顺序，必须唯一且合法；空元组表示全部剔除。执行层须在 `processing` 期间显式提交一次结果，身份不符、重复提交或终态后的迟到结果均拒绝。真实执行器仅在完整门禁通过且存在后续执行器时批准附件；正式 Agent Core 已接入该路径，不能仅因文件可读就默认准入。

附件元数据包含 `file_id/file_name/file_path/admission`，精确字段见 [SQLite 文件引用](../data/communication-sqlite.md#文件引用)。处理规则如下：

| 时机 | 文件处理 |
| --- | --- |
| 注册任务 | `pending`；字节只暂存内存，路径为 null，不创建 upload 文件。 |
| 明确通过准入 | `accepted`；确定 UUID 相对路径，仅取得后续保存资格，不立即写盘。 |
| 文件未通过准入 | `unavailable`；保留身份与名称，路径为 null，释放运行时与驻留层持有的该文件字节。 |
| 任务结束且附件仍待判断 | 转为 `unavailable` 并释放字节；即使任务 completed 也不推断通过。 |
| 整轮 rejected | 本轮全部新附件转为 `unavailable`，先前文件通过结果也不再赋予保存资格。 |
| 其他终态且文件已经通过 | 保留 accepted，允许后台备份；取消、替换或失败不撤销已有准入结果。 |

文件终态标记与任务终态在同一共享锁内提交，先预校验后释放字节；事件提交失败不会提前清理输入。明确筛选后，`get_input` 只返回保留附件，不再返回被剔除字节。执行器此前取得的局部引用仍需自行及时释放，服务无法撤销外部持有的 Python 引用。

原始轨迹备份先保存/校验本批已准入附件，再提交 SQLite 轨迹与工作区。文件写入采用排他创建、fsync 与内容校验，禁止覆盖不同内容；注册时避开已驻留/已存在的 UUID，身份冻结后若遭遇磁盘冲突则失败重试，不改名篡改原轨迹。写盘或数据库失败保留固定备份快照及已准入字节。拒绝附件不要求磁盘文件存在，也不会阻塞正常备份或归档。

accepted 只表示保存资格，不是“磁盘写入已确认”；异步备份未完成时路径对应文件可能尚不存在。备份成功不修改冻结 payload，已准入字节继续保留到记忆归档成功以支持文件校验和缺失恢复。归档与驱逐检查同样遵守准入状态，不能绕过规则补写待判断或不可用附件。

没有 admission 字段的旧记录沿用旧磁盘存储契约，不自动迁移、不删除已有附件，但读取接口不默认授予访问权。真实门禁已接入；附件准入由真实门禁决定。附件读取要求当前用户的对应任务轨迹已驻留且文件明确 accepted，优先内存、其次磁盘；即使会话已打开，未加载旧任务的文件仍不可访问，不自动查询数据库补齐授权。成功读取刷新会话活动计数，磁盘 I/O 后重新检查同一次驻留身份；完整协议见[资源文件 API](../../api/resource.md#读取已驻留会话任务的-pdf-附件)。

异常退出会丢失尚未备份的内存附件；文件系统与 SQLite 不是跨介质原子事务，文件已写入而数据库未提交时可能留下待重试文件，异常退出后的孤立文件回收仍未实现。“不落盘”指不持久化至业务 upload 目录，HTTP multipart 解析器仍可能使用会自动关闭清理的临时文件。

---

## 前端与模型窗口

| 操作 | 内部驻留范围 | 前端 records | 模型历史候选 |
| --- | --- | --- | --- |
| 首次打开 | 最新摘要及其后已落库记录，合并实时轨迹 | 该范围全部任务 | 最新摘要及其后记录 |
| 向前刷新 | 向前扩展至更早一条摘要，合并新增尾部 | 扩展后全部任务 | 仍从最新摘要开始 |
| 没有摘要 | 加载至会话起点 | 全部驻留任务 | 全部驻留记录 |

例如内部从 [摘要6, 任务7] 扩展为 [摘要3, 任务4, 任务5, 摘要6, 任务7]，前端仅返回 [任务4, 任务5, 任务7]，模型仍取 [摘要6, 任务7]。

前端返回保留原 sequence，不因过滤摘要重新编号。has_more 按内部边界计算；空任务列表仍可能有更早历史。model_context_start_sequence 可能指向未返回的摘要。返回值深拷贝，外部修改不污染驻留数据。

SQLite 查询显式选择记录元数据、payload、activated_at 与 processing_duration_ms，不读取 retrieval_text/embedding。新任务 created_at 为注册时间，activated_at 为首次激活 UTC 毫秒，结束时复制快照的总时长。老记录计时缺失为 null，不推算。

模型入口 get_model_records 仅提供窗口候选，状态过滤、token 预算和工作区组装仍需后续按[上下文规范](../../standard/agent-context-management.md)实现。

门禁使用独立内部入口 `CommunicationEventService.get_gate_history(conversation_id, turn_id, owner=...)`：先校验轮次所有权及 processing 状态，再持共享锁调用 `ConversationHistoryService.get_gate_records_locked`。只从当前轮之前、最新累计摘要之后选择最近最多五条有效终态任务，排除拒绝、过期、在途任务与摘要本身；返回深拷贝，包含已备份与 fresh 数据，不额外加载向量或检索文本。无合格历史则返回空元组，不跨摘要补足数量；有文字时上下文节点仍判断明确的先前文件操作意图，只有文件且无历史时跳过。具体输入投影和执行规则见[上下文相关性判断](../workflow/contract-communication/context-relevance.md)。此入口不改变用户 open/refresh 返回范围。第二层使用独立的[门禁后上下文装配](../workflow/contract-communication/context-assembly.md)，读取最新摘要后的全部已准入有效任务，不受五轮限制。

---

## 备份、删除与可靠性

终态提交只唤醒后台任务，默认让出 250 毫秒后尝试备份；失败等待 1 秒重试。同会话按 sequence 收集未备份的连续终态任务，遇活动任务停止收集。待备份数据直接从驻留记录中选取，不创建第二份用于问答或展示的历史队列。

在同一驻留锁内复制本批轨迹和当前工作区，再调用 `backup_tasks_with_workspace`，在一个 SQLite 写事务中保存全部轨迹并同步工作区。任一记录或工作区失败，整批回滚；成功才同时确认记录的 `_persisted` 标记和 `persisted_workspace_revision`。没有工作区内容变化时仍校验其快照一致性，但不虚增版本和更新时间。

备份在独立线程执行，写入时不持有轨迹锁，用户可继续接收输出、注册下一轮或提交工作区更新。成功不清理、不改写驻留轨迹，也不把旧工作区快照覆盖回内存；期间产生的新任务和新工作区版本继续保持待备份状态并唤醒下一次处理。

失败时保留原 `pending_backup` 快照，下一次先重试它，不将后来的更新混入旧批次。轨迹按原 record_id、turn_id、sequence 和冻结内容幂等确认；工作区按完整 payload、revision、updated_at 确认。这样即便数据库提交成功但回执丢失，也可以确认旧批次后继续保存较新的内存版本。读历史时偶然读到在途已提交记录，不提前改变该记录的 fresh 标记。单个会话失败不影响其他会话。

删除与备份通过单独的协调锁串行；SQLite 删除成功后同时驱逐会话轨迹和事件源，并停止该会话的执行协程。既有 SSE 被唤醒后以 turn_unavailable 关闭，不声称正常完成。失败保留内存，迟到输出不能复活已删除会话。附件与正式合同/ES 数据不在删除范围。

应用正常关闭先停止事件生产，把未结束轮次标为 failed，再排空旧备份及其后产生的新版本；发生失败则不无限阻塞关闭，记录不含密钥/正文的日志后释放内存。突然崩溃或备份持续失败时，fresh 轨迹和未备份工作区可能丢失，不承诺零丢失。正式服务跨重启保留已备份历史和工作区。

当前要求单进程、单 worker；已接入[十分钟记忆归档与空闲驱逐](communication-archive.md)，仍没有总驻留容量控制。SSE 的容量限制不等于完整历史容量限制，长期大量会话需要后续增加驻留预算。累计摘要生成、历史压缩及文件垃圾回收仍不包含在当前实现中。

---

## 工作区驻留与内部接口

首次打开或注册轮次时，轨迹窗口与工作区在同一 SQLite 读事务内载入。后续 open、refresh 和注册只合并历史，保留内存中的最新工作区，不能用尚未更新的数据库内容覆盖它。删除会话时统一清除轨迹、工作区和待备份副本。

内部接口供后续问答执行器使用，不是新增 HTTP 或模型工具接口：

- `get_workspace(conversation_id, secret_key=...)`：加载并返回工作区深拷贝，包含 payload、revision、updated_at。
- `update_workspace(conversation_id, secret_key=..., payload=..., expected_revision=...)`：校验归属、完整结构及内存版本，接受后递增 revision，更新时间并唤醒后台备份；返回深拷贝。过期版本拒绝提交。
- `create_workspace_entry(conversation_id, secret_key=..., section=..., entry=..., expected_revision=...)`：创建用户补充（section=task_constraints，entry 为文本）、已知信息或剩余方向，程序生成键并返回 `(key, snapshot)`；不能直接创建已探索记录。
- `update_workspace_field(conversation_id, secret_key=..., section=..., key=..., field=..., value=..., expected_revision=...)`：修改已有字段。task_constraints 中 key=task 修改用户任务，其他 key 定位已有补充，此时 field 为 null；其余区域按条目键与字段更新。
- `complete_workspace_direction(..., direction_id=..., outcome=..., conclusion=..., information_ids=..., expected_revision=...)`：同一提交将剩余方向移入已探索区，保留 ID 并记录结果；失败不移除原规划。
- `remove_workspace_entry(..., section=..., key=..., expected_revision=...)`：移除失效补充、已知信息或方向条目；不允许留下悬空信息引用，不删除原始轨迹。
- `flush()`：每个会话最多尝试一个备份批次；返回值只表示本轮尝试是否成功，不代表处理期间新产生的数据已经全部保存。

内容契约复用 `WorkspacePayload` 的四个 KV 区域（用户任务及补充、已知信息、已探索方向、剩余方向），见 [SQLite 工作区](../data/communication-sqlite.md#conversation_workspaces会话工作区)。结构校验不等于业务确认：目标、已知信息和后续任务由未来问答模型及其执行器整理，当前服务不根据轨迹猜测工作区，不读取失败草稿或私有推理填充它。

创建、字段更新、移除和探索收束均复用完整快照校验与版本提交；读取后出现竞争更新时拒绝旧版本，不覆盖其他有效条目。信息状态与探索结果的业务真实性仍由执行器判断，服务不会因用户任务改变而自动删改相关规划。[模型可见工具](../workflow/contract-communication/workspace-tools.md)已提供定义和执行适配，通过受权提交回调复用完整 payload 更新；[模型循环](../workflow/contract-communication/agent-runtime.md)已接入。

工作区是会话级当前状态，不是每条任务的历史副本；备份保存的是取快照时已接受的最新内容，不宣称它恰好覆盖到本批最后一条任务。即使没有新轨迹，已接受但尚未备份的工作区更新也会独立补存，避免备份期间产生的新版本永久遗漏。

工作区不写入任务 payload、公开 trace、SSE 或前端 open/refresh 响应。现有前端契约不变；模型历史入口仍只提供最新摘要及其后任务，主循环每轮读取最新工作区并组装模型上下文。驻留会话的工作区不得绕过服务直接更新数据库，否则备份会报版本冲突并保留内存，不静默覆盖。

原始备份继续采用终态唤醒机制；记忆加工和空闲驱逐由已接入的[归档服务](communication-archive.md)独立调度，不将“已备份”当成“已加工”。工作区内容仍不由后台模型整理。

---

## 验证

本地 unittest 覆盖实时读取、跨工具消息片段、SSE 缓存截断、终态冻结、取消/替换/过期、计时、附件唯一命名、后台自动备份、失败与幂等重试、新轮次不等待备份、历史窗口、用户隔离、删除防复活及已备份内容重新加载。

`tests/test_communication_workspace.py` 另覆盖工作区载入、深拷贝与非公开边界、内容/版本校验、同事务回滚、重启恢复、六种终态同步备份、回执丢失后的固定快照重试、备份期间更新不丢失和关闭排空。

---

## Agent Core 上下文读取

工作区与任务仍只维护一份驻留数据。正式门禁通过后，记录 agent_core_ready 和已确认文件页数，在共享锁内读取工作区、最新结构化摘要及其后的任务副本，在锁外渲染。工作区版本提交后重新读取即取得新值；具体接口与过滤边界见[门禁后上下文装配](../workflow/contract-communication/context-assembly.md)。


---

## 自动摘要与驻留排序

`insert_agent_summary` 接受预期旧摘要、已验收新摘要和 task_id 前缀范围。持备份锁与驻留锁校验活动任务、旧摘要基线、完整前缀及 completed 末项后，将摘要插在最后一个被压缩任务之后，并顺延后续记录的 sequence。已压缩任务仍留在同一份历史中供展示与归档；模型下次装配只取最新摘要及其后任务。

已持久化任务同样可能需要顺延。下一次备份携带全部驻留 record_id 的目标位置，在同一事务保存排序、新摘要、冻结任务和工作区。失败快照的重试也须包含新摘要，不能只更新排序；详见[SQLite 插入契约](../data/communication-sqlite.md#部分历史压缩后的摘要插入)。再次 open/refresh 时按稳定 record_id 合并，尚未备份的新驻留序号优先，不能把磁盘旧序号误报为冻结内容变化。

摘要验收和内存插入先于执行前压缩分支的原工具执行；压缩失败不改边界，原工具不执行。工具执行后才压缩的分支即使失败，也保留已接受工具状态。工作区提交在共享锁内校验活动任务和 revision，成功反馈前更新唯一内存副本。


---

## 终态轨迹清理

统一终态投影在冻结前移除 agent_messages 中来源已验证的独立系统操作提示，并清理公开 trace 中的 system_guidence 条目。成功调用及反馈原样保留；不扫描正文标签，不改写工具结果。终态校验通过后才替换驻留记录，因此后续备份和重新加载直接得到清洁轨迹。活动任务的提示仍用于操作反馈和容量计数，私有审计不受终态清理影响。见[任务结束后的提示清理](../workflow/contract-communication/context-assembly.md#任务结束后的提示清理)。
