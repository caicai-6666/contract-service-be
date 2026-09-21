# Communication SQLite 存储

## 用途与实现边界

数据库默认位于 `data/communication/communication.db`，包含会话、任务/摘要记录、工作区、任务检索投影及独立思考窗口五张表。应用启动时幂等初始化，并通过 `application.state.communication_store` 暴露 `SQLiteCommunicationStore`。

已实现建表、会话及空工作区的原子创建、终态任务追加、累计摘要追加、最近摘要边界读取、工作区版本更新和会话所有权校验；存储层已支持三入口检索投影、加工标记与工作区原子提交；自动加工与调度已接入新契约。历史不依赖 SSE 缓存过期；数据库重开后记录仍在。

> **接入边界：** 会话创建、历史加载和事件运行时已接通统一驻留轨迹；任务终态后台复制至 SQLite，附件注册时仅暂存内存，明确准入后随终态备份保存至 upload。见[历史驻留与备份](../system/communication-history.md)。已支持本人已驻留任务的准入附件读取；主助手生成循环、结构化摘要及上下文装配已接入；进程重启后自动续跑活动任务与向量检索仍未实现。

---

## 业务表与扩展窗口

### conversations：会话

| 字段 | 含义 |
| --- | --- |
| `conversation_id` | 会话主键。 |
| `secret_key` | 当前用户配置密钥原值，作为所属用户标识，不增加用户表。 |
| `created_at` | UTC Unix 毫秒时间戳。 |
| `name` | 用户可修改的展示名称，默认按创建时间生成北京时间 `YYYY-MM-DD HH:mm:ss`；自定义名称去除首尾空白后为 1 至 200 个字符。 |

`create_conversation(..., name=None)` 未提供名称时，用同一次采集的 `created_at` 按固定 UTC+8 生成名称，例如 `2026-09-08 15:34:06`，不依赖服务器或容器时区；传入名称则保留自定义值，空白名称仍拒绝。默认值由存储方法生成并显式写入，不能绕过方法依赖旧库遗留的 SQL 默认值。名称不会随时间或重启更新。

`rename_conversation` 只允许所有者改名，`read_conversation` 返回 ID、名称和创建时间，不返回密钥。改名不改变创建时间、所有权或任务顺序。启动时会为尚无名称列的已有库补列并按原创建时间生成名称；已有名称（包括旧的“新会话”）不覆盖。已接入 [PATCH 改名接口](../../api/communication.md#修改会话名称)。

`delete_conversation` 已接入 [DELETE 会话接口](../../api/communication.md#删除会话)，按当前密钥校验所有权后事务性级联删除会话、任务/摘要和工作区。清理 SQLite 后同步驱逐会话轨迹和运行时轮次，并与在途备份串行；不删除附件或正式合同数据。

索引 `(secret_key, created_at, conversation_id)` 支持按用户和时间查询。标识由后端从已认证的用户对象取得，不能信任模型或前端自报的归属。读取结果不返回密钥，错误不回显密钥。

`list_conversations(secret_key=...)` 按创建时间降序、同时间按会话 ID 降序返回该用户全部会话，仅包含 ID、名称、创建时间。已接入 [GET 会话列表接口](../../api/communication.md#获取当前用户会话列表)，不依赖内存轮次或后台轨迹备份状态。

**身份约定：** 密钥长期固定、全局唯一且永不分配给其他人。停用用户不会自动删除历史；新增不同密钥的用户不影响旧记录。不因用户名称变化改变归属，也不按名称迁移历史。系统只校验当前配置的密钥唯一性，无法自动识别停用密钥被分配给另一人的情况；运维必须保证不复用。

数据库保存的是登录凭据原值，不是密码哈希。新建数据库在 POSIX 上使用 `0600` 权限；既有库权限不自动修改。数据库、日志侧文件和备份必须按敏感数据保护，不能进入 Git、模型上下文或 API 响应。若以后改变密钥，必须另行迁移会话归属，不能期待自动关联。

### conversation_records：任务与摘要

| 字段 | 含义 |
| --- | --- |
| `record_id` | 程序生成的 UUID 主键。 |
| `conversation_id` | 会话外键，随会话删除级联清理。 |
| `sequence` | 会话内唯一递增顺序，任务和摘要共用；不依赖时间戳区分先后。 |
| `kind` | `task` 或 `summary`。 |
| `turn_id` | 任务关联现有轮次 ID，全库唯一；摘要为空。 |
| `status` | 任务终态；摘要为空。 |
| `payload` | 一个 JSON 对象，完整打包该任务或摘要内容。 |
| `created_at` | 记录创建时间，UTC Unix 毫秒；实时任务为注册时间，旧 append_task 记录为追加时间。 |
| `activated_at` | 首次激活时间，UTC Unix 毫秒；旧记录未知或未激活时为空。 |
| `processing_duration_ms` | 任务总处理时长，非负整数毫秒；摘要及缺少可靠计时的旧记录为 null。 |
| `memory_processed_at` | 三入口检索投影成功提交时间，UTC Unix 毫秒；空值代表待加工，摘要为空。旧综合摘要标记在迁移时清空，等待重新加工。 |

一条任务对应一轮用户请求，不是内部子任务。任务内部按[有序轨迹设计](../workflow/contract-communication/turn-trace.md)组织输入、用户可见说明、调用摘要及最终答复。当前存储层只校验 `payload` 为合法 JSON 对象，内部轨迹 Schema 与语义校验由后续收集层负责，不能把存储成功等同于模型内容通过校验。

`append_task` 只追加终态记录，活动轮次暂时留在内存；重复 `turn_id` 拒绝写入，不静默覆盖。顺序按提交顺序分配，后续收集层须保证同会话按轮次提交。`cancelled`、`superseded` 分别对应已有的手动终止、用户目标调整语义；面向模型的过滤不在此层执行。

`append_task(processing_duration_ms=...)` 接收快照终态固定的同名计时值：从首次激活到终态，不含待激活等待和后台备份耗时；未激活即结束传 0。兼容调用缺省为 null，不以 created_at 估算。字段独立于 payload，历史加载和前端任务记录均返回。启动时为旧库补列，原记录保持 null；运行时备份已直接复制快照值，不以后台执行时刻重新计算。

实时任务使用 `backup_tasks_with_workspace` 按会话原子备份轨迹与工作区：按注册时的 record_id、turn_id、sequence、创建时间和冻结内容写入；相同内容重试视为成功，不同内容报冲突，任一失败则整个事务回滚。基础 `backup_task`、`append_task` 仍供离线显式操作，禁止在有 fresh 任务的会话绕开统一驻留层写入。调度、重试、删除和关闭边界见[历史驻留与备份](../system/communication-history.md)。

索引覆盖会话内顺序、会话内时间、摘要边界及未加工任务部分索引 `records_memory_pending`。`read_memory_backlog` 只取未加工原轨迹及已加工 ID，不加载检索正文或向量。原始任务备份与历史加载保持独立。

### conversation_task_retrievals：三入口检索投影

一条任务对应最多一行检索投影；通过 `record_id` 外键关联，随原任务/会话删除级联清理。摘要记录不能写入本表。

| 字段 | 含义 |
| --- | --- |
| `record_id` | 主键兼任务外键，关联 conversation_records.record_id；不使用可能因摘要插入而改变的 sequence。 |
| `user_input_text` | 用户问题、文件名、展示名称和文件摘要的可读投影，不含 file_id/page_count 元数据。 |
| `user_input_embedding` | 用户文字与每份文件格式化内容分别编码、归一化后等权平均，再次归一化的融合向量。 |
| `intermediate_output_text` | 按编号模板组织的已完成公开中途输出；原条目及位置回连原始任务，模板见检索文本模板主文档。 |
| `intermediate_output_embedding` | 每条中途输出单独编码并归一化，等权平均后再次归一化的融合向量。 |
| `final_output_text` | 实际完成的最终答复，没有则为空。 |
| `final_output_embedding` | 最终答复对应向量。 |
| `created_at` | 首次成功写入时间，UTC Unix毫秒；幂等重试不改变。 |

三类正文的精确标签、换行、缺失字段规则、独立编码单元及指令，统一见[检索文本模板与向量化契约](../workflow/conversation-memory/retrieval-embedding.md)。英文原始字段key与中文格式化标签属于不同层，数据库保存的是格式化正文，不是附件JSON。

每个区域的文本/向量必须同时存在或同时为NULL，不能用空字符串或零向量占位。允许全NULL行表示外层筛选跳过或经过加工确认没有可检索区域；“没有行”表示尚未提交加工结果，不得因模型调用失败写全NULL行。文本内容是否符合格式及事实，由后续加工器负责；存储层不能仅凭向量判断是否由正确原文生成。

每个向量固定4096维，以16384字节float32 BLOB保存。SQL检查配对、非空文本、BLOB长度、外键和任务类型；Python `TaskRetrievalRecord` 进一步校验有限数值及L2单位范数，拒绝布尔数值。Python存储接口使用tuple向量。中途条数不会增加外层入口权重；这里不保存任务综合向量，不创建FTS5或ANN索引。

`archive_tasks_with_workspace(..., records, memories, workspace, expected_workspace_revision)` 的 memories 现为 `TaskRetrievalRecord` 字典列表，与 records 逐条同序配对。原轨迹、检索投影、加工标记和工作区在同一事务提交；原记录身份/冻结内容由备份接口校验。若编码期间插入摘要导致sequence重排，按record_id读取当前序号，仅调整位置，不改变其余原始字段。相同内容重试幂等，不同检索投影报冲突，任何失败整批回滚。旧 `MemoryPendingRecord` 综合摘要结构明确拒绝，不映射到某个入口。

`read_task_retrieval(conversation_id, secret_key, record_id)` 校验会话所有权和任务归属，返回三组文本/解码后的向量及写入时间；任务未加工返回None，任务不存在或不属于该会话时报不存在。此接口不是语义检索，普通会话历史加载不读取检索表。

### 旧库迁移与分阶段边界

启动 `initialize()` 在同一事务重建旧 conversation_records，移除 retrieval_text、embedding 及遗留 embedding_model，保留原任务/摘要、payload、顺序、计时、会话归属和工作区，并恢复索引。旧综合检索内容不复制进新表，因为不能可靠拆解成三入口；原 memory_processed_at 一并清空，所有旧任务待重新加工。迁移失败回滚，成功后重复初始化不会清空已写入的新投影。

新子表在父表迁移完成后创建，避免父表重建误触发新检索行级联删除。表结构不逐行保存模型名称；同一向量空间必须统一模型、维度和编码规则，更换不兼容模型需重建。

单任务三入口格式化、向量计算、外层筛选、并发调度与原子提交均已接入，正式bootstrap启动归档扫描及安全驱逐。原任务和工作区的后台备份独立运行，不等待向量化。旧摘要图及兼容入口已移除。旧任务在重新打开所属会话后按待加工标记重新处理，不主动打开全部未驻留会话。详见[归档与驱逐](../system/communication-archive.md)。

验证覆盖 `tests/test_communication_store.py`、`tests/test_communication_retrieval_store.py`，包括新建/重复初始化、旧库迁移、三组向量恢复、缺失区域、SQL/Python约束、所有权、幂等冲突、事务回滚与级联删除。

### conversation_workspaces：会话工作区

| 字段 | 含义 |
| --- | --- |
| `conversation_id` | 同时为主键和会话外键，保证一个会话一个工作区。 |
| `payload` | `achieved_goals`、`known_information`、`next_tasks` 三个字符串列表。 |
| `revision` | 从 0 开始的内容版本，每次接受工作区更新加 1；批量备份可跨多个内存版本，重复备份不递增。 |
| `updated_at` | 工作区内容最近被接受的时间，UTC Unix 毫秒；备份保留原值，不改成落库时间。 |

创建会话时原子创建三个空列表。更新必须携带读取时的 `expected_revision`，过期版本抛出 `CommunicationStoreConflict`，避免并发覆盖新信息。只做结构校验，证据、权限和业务有效性必须由调用层完成。

驻留会话通过 `ConversationHistoryService.update_workspace` 提交内存更新，不直接调用存储层的 `update_workspace`。每批轨迹备份同步保存所捕获的工作区版本，通过上次已持久化版本校验防止覆盖外部更新；一致快照可幂等确认。工作区初始化、驻留和同步落库已实现，模型自动整理尚未实现。

工作区保存达成的目标、已知事实和后续任务，不是对话原文、模型草稿或私有审计。写入前应遵循[上下文与记忆规范](../../standard/agent-context-management.md)，错误调用和半成品不能成为权威事实。本次没有新增模型节点或修改纠错循环。

---

## Payload 契约

> **契约与实现：** 实时投影已按以下契约收集消息和显式工具调用并进行终态备份；基础存储接口仍只校验合法 JSON 对象，不对外部离线 payload 执行完整轨迹语义校验。现有 HTTP/SSE 快照保持独立投影。

### 任务对象

`kind=task` 时，新任务顶层保存 `input`、`trace`、`events` 和 `event_cursor`。`trace` 为模型使用的有序内部轨迹，`events` 为用户界面恢复使用的精简 SSE 记录，正文允许在两种用途之间重复存储。不设置版本号或独立的 `messages` 列表。以下先展示内部 input/trace 结构，展示事件契约见下文。

```json
{
  "input": {
    "text": "分析合同付款条件",
    "files": [
      {
        "file_id": "a7f83b90-5ac2-4e16-8f92-71677c450dc1",
        "file_name": "采购合同.pdf",
        "display_name": "生产线设备采购合同",
        "summary": "约定生产线设备的采购范围、付款节点、交付验收和质保责任。",
        "file_path": "/a7f83b90-5ac2-4e16-8f92-71677c450dc1.pdf",
        "admission": "accepted"
      }
    ]
  },
  "trace": [
    {
      "sequence": 1,
      "type": "message",
      "message_id": "m1",
      "message_kind": "intermediate",
      "text": "我先读取付款条款。",
      "status": "completed",
      "references": []
    },
    {
      "sequence": 2,
      "type": "tool_call",
      "call_id": "call-001",
      "name": "read_contract_clauses",
      "title": "读取付款条款",
      "input_summary": "读取付款与验收约定"
    },
    {
      "sequence": 3,
      "type": "tool_result",
      "call_id": "call-001",
      "status": "succeeded",
      "output_summary": "合同约定预付款比例为30%",
      "references": []
    },
    {
      "sequence": 4,
      "type": "message",
      "message_id": "m2",
      "message_kind": "final",
      "text": "合同的预付款比例为30%。",
      "status": "completed",
      "references": []
    }
  ]
}
```

- `input.text` 保存用户原文，没有文字时为 null；`input.files` 没有附件时为 []。
- `trace` 为唯一交互顺序，内部 `sequence` 从 1 连续递增，无输出时可以为空。
- `message` 条目直接包含文本、完成状态和引用，`message_kind` 取 `intermediate/final`，终态 `status` 取 `completed/interrupted`，活动驻留允许 streaming。终态备份中不保留 streaming 状态。
- 同一消息因工具穿插分为多个片段时，保留原位置、共用 `message_id`，不重复全文；冻结时各片段统一记录整条消息的最终状态与消息级引用。引用可按 ID 去重，但不能为聚合消息而重排轨迹。
- `tool_call` 保存调用 ID、名称、展示标题和非敏感输入摘要；`tool_result` 通过相同 `call_id` 关联此前调用，状态为 `succeeded/failed/interrupted`，中断时 `output_summary` 可为 null。不得虚构成功结果。
- `references` 始终是列表，每项只包含 `type` 和 `location`，没有引用为 []；具体点击行为见下文。

### 展示事件备份

- `events` 从注册时的 `[]` 开始，保存所有已接受的 `turn.status/task.progress/message.completed/error` 业务事件，不保存 `message.delta`、心跳和连接级错误。
- 每项为 `sequence/event/data`；`sequence` 是真实 SSE 序号，过滤 delta 后允许缺号。`data` 由与 SSE 相同的编码函数生成，保留正式引用及状态事件的计时，中间事件不增加时间。
- `event_cursor` 从 0 开始，每条有效事件（包括 delta）更新。用于活跃任务恢复后的订阅续接，不能以 events 最后一项的序号替代。
- delta 只更新内存累积正文及内部 trace，不逐条进入展示事件。消息正常结束保存 `message.completed(status=completed)`；中断时保存完整累积正文的 `message.completed(status=interrupted)`，随后保存任务终态。
- 中断收束和终态在共享锁下整批验证、原子更新，不从有界 SSE 缓存事后重建；终态随整个 payload 与工作区一并备份。
- 内部 `trace` 引用仍为 `type/location`；新 `events.data.references` 原样保留 SSE 的 `document_id/page_number`，不通过旧 trace 反推，避免丢失页码。
- 只扩展 JSON payload，不新增数据库列或表。旧数据不回填虚构事件；返回用户时按 [用户展示 Payload](../../api/communication.md#用户展示-payload) 执行兼容投影。模型提示词仍只渲染 input/trace，不渲染 events。

### 结果引用

消息和工具结果共用以下引用结构，不再存储引用标题、文档 ID 或页码：

```json
{
  "references": [
    {
      "type": "contract",
      "location": "/a7f83b90-5ac2-4e16-8f92-71677c450dc1.pdf"
    },
    {
      "type": "web",
      "location": "https://example.com/article"
    }
  ]
}
```

| type | location 含义 | 前端点击行为 |
| --- | --- | --- |
| `contract` | 合同文件保存地址，而不是网页链接或 resource 请求 URL。 | 携带该地址请求 resource 路由下的相关接口获取 PDF，再打开预览。 |
| `web` | HTTP/HTTPS 网页链接。 | 直接跳转网页。 |

会话上传文件的 `location` 使用 `/{file_id}.pdf`，相对于 `data/communication/upload/`；已入库合同使用其现有文件保存地址。`contract` 类型同时涵盖两者，不额外增加展示类型。resource 层后续须明确区分存储位置，限定允许访问的根目录并校验归属或权限，不能将传入地址直接作为任意磁盘路径读取。引用必须来自实际读取或检索到的来源，不得虚构。

以上是历史 payload 的已确认设计。当前 SSE、展示快照的引用仍采用既有 `document_id/page_number` 契约；本次不改动它们，也不表示 upload 文件读取已接入 resource。接口字段调整与文件地址解析规则后续单独实现并同步 API 文档。

### 文件引用

服务端生成 UUID 作为 `file_id`，磁盘名称为 `{file_id}.pdf`，`file_name` 保留原始展示名称。新附件增加 `admission` 字段：

| admission | file_path | 含义 |
| --- | --- | --- |
| `pending` | null | 内存暂存，尚未取得准入结果；只存在于活动任务。 |
| `accepted` | `/{file_id}.pdf` | 已允许持久化；实际文件随终态轨迹异步备份，不表示此刻写盘已确认。 |
| `unavailable` | null | 未通过或未判断就结束；名称与 UUID 仍用于历史展示，不保留可访问路径和文件字节。 |

`input.files` 同时保存后端生成的 `display_name`（内容名称）与 `summary`（内容摘要）。新附件注册时两项为 null；正式门禁返回完整、已校验的摘要批次后，执行层在准入决策和任务终态前调用 `record_file_summaries` 一次性写入。通过原始上传下标及原文件名校验，绑定已有 `file_id`，不改写用户原文、原文件名、上传顺序或 UUID；同名文件不按名称匹配。只复制这两个轻量字段，不复制 reasoning、节点审计、PDF 或图像。

摘要写入与附件准入相互独立：后续相关性拒绝时可以保留已生成的描述供用户历史展示，但附件仍不可用、不落盘；拒绝轮次仍不应进入第二层上下文。门禁返回前取消、摘要失败或未执行时保留 null，不写入部分批次或迟到结果。终态冻结后禁止更新。原有终态备份、归档和历史读取会原样保留两个字段，无需新增 SQLite 列或迁移；旧记录缺字段时保持缺省，不伪造摘要。

模型历史候选可读取上述字段，但第二层提示词组装仍未实现，本次也不改变记忆加工节点的提示词渲染策略。

路径以 `data/communication/upload/` 为逻辑根，不是操作系统绝对路径，也不包含会话子目录。旧记录没有 admission 时保留原契约，不推断旧门禁结论、不迁移或删除其文件。该字段位于 JSON payload，不新增数据库列。

准入接口、终态规则、排他写入、重试与异常边界统一见[附件准入与延迟落盘](../system/communication-history.md#附件准入与延迟落盘)。[资源读取接口](../../api/resource.md#读取已驻留会话任务的-pdf-附件)校验本人已驻留任务和明确准入，优先内存，磁盘回退限定在 upload 内，禁止越界路径；磁盘存在或仅知道 ID 都不构成授权。会话删除后的附件清理策略尚未实现。

### 摘要对象

`kind=summary` 时使用以下独立结构：

```json
{
  "text": "已分析付款比例，下一步关注验收条件。",
  "covers_through_sequence": 12
}
```

摘要对象只包含 `text` 和 `covers_through_sequence`，不设置版本号。`text` 为非空累计摘要，覆盖序号指向会话记录表的 `sequence`，不是任务内部 `trace.sequence`。

### 与其他状态的分工

会话 ID、轮次状态、创建时间和会话顺序以数据库列为准。内部 input/trace 不保存替代关联；通过会话逻辑顺序结合 `status=superseded` 表达用户方向调整，不按随机 UUID 排序。展示 events 原样保留 SSE 中的 turn_id、状态、时间及 superseded_by_turn_id，以保证实时与恢复一致，不作为模型上下文的额外关系字段。

运行接口用于指定替代目标的字段保持不变。`CommunicationSnapshot.messages` 继续用于前端恢复，不属于本次删除的历史 payload 列表。工作区、检索文本和向量独立存储。终态冻结与后台复制规则见[有序轨迹设计](../workflow/contract-communication/turn-trace.md)。

---

## 摘要与恢复边界

例如顺序为 `任务 1 → 任务 2 → 摘要 3 → 任务 4 → 任务 5`，恢复读取返回 `摘要 3、任务 4、任务 5`，包含摘要，按正序返回，不删除更早历史。

摘要的 `payload` 包含 `text` 和 `covers_through_sequence`。摘要必须是累计摘要：既覆盖此前摘要所代表的历史，也覆盖它之后到声明边界的全部任务。存储层校验摘要非空、覆盖边界等于当前尾部，但无法验证文字是否真的完整覆盖语义。

`append_summary(..., expected_sequence=...)` 在同一写事务中比较尾部并插入；生成摘要期间如果出现新记录，拒绝旧摘要，调用方必须重新读取并生成，防止摘要位置跳过未覆盖任务。

`read_context_records` 使用索引定位最新摘要并只读取该摘要及其后记录；没有摘要时读取全部现有记录。它返回候选历史，不直接送入模型：后续仍需执行取消/替换过滤、token 预算和权限校验。频繁摘要或极长单轮的预算策略尚未实现，不承诺任意历史长度都能放进上下文。

---

## 使用与配置

配置项 `COMMUNICATION_DATABASE_FILE` 支持绝对路径；相对路径以项目根目录为基准。独立部署已挂载整个 `data` 时会覆盖此目录。底层复用 [SQLite 向量连接](../../capability/infrastructure/sqlite-vector.md)，每次连接启用外键，写操作使用短事务，不维持跨请求连接。

```python
from pathlib import Path
from pydantic import SecretStr
from app.infrastructure.communication_store import SQLiteCommunicationStore

store = SQLiteCommunicationStore(Path("data/communication/communication.db"))
store.initialize()
# 实际调用应从认证依赖取得 SecretStr，不能硬编码生产密钥。
owner = SecretStr("example-only")
store.create_conversation("example-conversation", secret_key=owner)
store.append_task(
    "example-conversation", secret_key=owner, turn_id="example-turn",
    status="completed", payload={"input": "示例问题", "trace": []},
)
```

接口为同步存储方法，未来从异步请求调用时应在线程中执行，避免阻塞事件循环。没有自动清理、用户管理、私有审计表或生产数据迁移；后续表结构变更应增加明确迁移，不依赖 `CREATE TABLE IF NOT EXISTS` 修改已有列。

本地 `tests/test_communication_store.py` 覆盖四表重开、用户隔离、最近摘要边界、过期摘要拒绝、工作区版本竞争、并发序号、非法内容回滚、外键和级联。测试文件沿用项目不追踪约定。

---

## 部分历史压缩后的摘要插入

主循环通过 `ConversationHistoryService.insert_agent_summary` 将验收后的 `fifo-topic-summary-v2` 累计摘要插入驻留轨迹，位置紧随实际压缩前缀的最后一个任务。后续记录的 sequence 顺延，record_id、turn_id、原任务 payload 与检索加工数据不变。不能把部分历史摘要追加到会话尾部，否则最新摘要边界会遮蔽仍需使用的未压缩任务。

下一次 `backup_tasks_with_workspace(..., positions=...)` 在同一事务中完成：

1. 依照 record_id → sequence 映射移动已持久化记录。先暂移到所有现有/目标序号之外的正整数位置，再设置最终位置，避免唯一索引的中间冲突。
2. 备份新摘要与尚未落盘的冻结任务，保留原身份；相同快照重复提交仅确认一致内容。
3. 提交工作区快照及版本校验。任一步失败，整批排序、摘要、任务和工作区一起回滚。

该映射只覆盖驻留记录，较早未加载记录不受影响。当前采用单进程驻留写入边界；冲突不会静默覆盖。插入与备份共享备份锁，不能让在途旧排序快照覆盖新状态。已有失败备份等待重试时，保留其工作区版本快照，同时纳入新摘要、待保存冻结任务及新排序，避免分两次提交导致摘要暂时缺位。

`append_topic_summary(summary=..., expected_sequence=...)` 是结构化摘要的独立尾部追加接口，要求完整当前尾部已被覆盖；主循环部分 FIFO 压缩不使用它。读取继续按 sequence 最大的摘要及其后记录加载，无需另一份压缩范围索引。

原生成功交互存入任务 payload.agent_messages；它与公开 trace/events 分开，只有完整工具调用、反馈与来源明确的系统提示能写入。任务终态后冻结，渲染字符串不持久化。

验证见 `tests/test_agent_summary_persistence.py`：混合已持久化和内存任务、失败快照重试、事务回滚及新服务实例重新加载摘要边界。


---

## 原生思考窗口存储

新增 conversation_reasoning_windows 表，conversation_id 为主键并引用 conversations，删除会话时级联清理；payload 保存完整窗口 JSON（版本、下一个绝对位置及有限思考条目）。启动期幂等建表，旧会话没有记录时按空窗口处理。写入校验所有权和期望位置，独立于任务/工作区批量备份；窗口数据不进入公开历史投影。范围与重启限制见[原生思考 FIFO 窗口](../workflow/contract-communication/reasoning-window.md)。


---

## 任务输入中的合同引用快照

任务 `payload.input.contracts` 保存有序的 `{document_id, file_name, summary}` 列表，旧任务缺省为空列表。快照由请求入口从正式合同 SQLite 读取，随原任务备份与归档原样保存，不增加新表或外键；正式合同后续修改或删除不改写既有任务。公开历史与 Agent Core 使用相同快照。

检索投影将引用合同作为文件区块处理：只提取文件名和非空摘要，不提取合同 ID，不补造展示名称或页数；独立编码后与用户文字及上传附件共同融合到 `user_input_embedding`。详见[检索文本模板](../workflow/conversation-memory/retrieval-embedding.md#用户输入存储模板)。
