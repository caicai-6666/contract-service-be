# Communication SQLite 存储

## 用途与实现边界

数据库默认位于 `data/communication/communication.db`，只包含会话、任务/摘要记录、工作区三张业务表。应用启动时幂等初始化，并通过 `application.state.communication_store` 暴露 `SQLiteCommunicationStore`。

已实现建表、会话及空工作区的原子创建、终态任务追加、累计摘要追加、最近摘要边界读取、工作区版本更新和会话所有权校验；已支持记忆加工标记、检索文本/向量与工作区的原子归档。历史不依赖 SSE 缓存过期；数据库重开后记录仍在。

> **接入边界：** 会话创建、历史加载和事件运行时已接通统一驻留轨迹；任务终态后台复制至 SQLite，输入附件注册时保存到 upload。见[历史驻留与备份](../system/communication-history.md)。真实工作流恢复、摘要生成、向量检索、附件资源接口和模型上下文组装仍未实现。

---

## 三表结构

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
| `retrieval_text`、`embedding` | 已接通归档写入的检索文本和 float32 BLOB；无记忆或未向量化时均为空。 |
| `memory_processed_at` | 记忆加工成功提交时间，UTC Unix 毫秒；空值代表待加工。跳过和无记忆也写入标记，摘要为空。 |

一条任务对应一轮用户请求，不是内部子任务。任务内部按[有序轨迹设计](../workflow/contract-communication/turn-trace.md)组织输入、用户可见说明、调用摘要及最终答复。当前存储层只校验 `payload` 为合法 JSON 对象，内部轨迹 Schema 与语义校验由后续收集层负责，不能把存储成功等同于模型内容通过校验。

`append_task` 只追加终态记录，活动轮次暂时留在内存；重复 `turn_id` 拒绝写入，不静默覆盖。顺序按提交顺序分配，后续收集层须保证同会话按轮次提交。`cancelled`、`superseded` 分别对应已有的手动终止、用户目标调整语义；面向模型的过滤不在此层执行。

`append_task(processing_duration_ms=...)` 接收快照终态固定的同名计时值：从首次激活到终态，不含待激活等待和后台备份耗时；未激活即结束传 0。兼容调用缺省为 null，不以 created_at 估算。字段独立于 payload，历史加载和前端任务记录均返回。启动时为旧库补列，原记录保持 null；运行时备份已直接复制快照值，不以后台执行时刻重新计算。

实时任务使用 `backup_tasks_with_workspace` 按会话原子备份轨迹与工作区：按注册时的 record_id、turn_id、sequence、创建时间和冻结内容写入；相同内容重试视为成功，不同内容报冲突，任一失败则整个事务回滚。基础 `backup_task`、`append_task` 仍供离线显式操作，禁止在有 fresh 任务的会话绕开统一驻留层写入。调度、重试、删除和关闭边界见[历史驻留与备份](../system/communication-history.md)。

索引覆盖会话内顺序、会话内时间、摘要边界及未加工任务部分索引 `records_memory_pending`。`archive_tasks_with_workspace` 已支持检索文本、4096 维 float32 向量、加工标记和工作区的原子写入。归档读取 `read_memory_backlog` 只取未加工原轨迹及已加工 ID，不加载已有检索正文/向量。详细规则见[归档与驱逐](../system/communication-archive.md)。后续跨会话向量查询可以先通过会话归属联接筛选，再按记录时间限定候选；当前未实现查询接口或 ANN 索引。

不再逐行存储 `embedding_model`。同一检索空间须统一模型、维度和编码规则；以后更换不兼容的模型时，需要统一重建向量，不能将不同模型的向量直接混合比较。向量化遵循[双侧指令契约](../workflow/conversation-memory/retrieval-embedding.md)。启动初始化会事务性移除旧库的该列及其约束引用，保留其余字段、记录、索引和外键；并补充加工标记，已有检索对的旧任务标为已加工，空检索字段的旧任务仍待处理。

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

`kind=task` 时，顶层只保留 `input`、`trace`。不设置版本号或独立的 `messages` 列表，也不重复存储最终答复。

```json
{
  "input": {
    "text": "分析合同付款条件",
    "files": [
      {
        "file_id": "a7f83b90-5ac2-4e16-8f92-71677c450dc1",
        "file_name": "采购合同.pdf",
        "file_path": "/a7f83b90-5ac2-4e16-8f92-71677c450dc1.pdf"
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

服务端生成 UUID 作为 `file_id`，磁盘名称为 `{file_id}.pdf`，`file_name` 保留原始展示名称。`file_path` 固定为 `/{file_id}.pdf`，以 `data/communication/upload/` 为逻辑根，不是操作系统绝对路径，也不包含会话子目录。

写文件采用排他创建，冲突时重新生成 UUID 并重试，禁止覆盖既有文件。读取必须限定在 upload 内并校验会话归属，禁止 `..` 等越界路径，不能仅凭文件 ID 获得其他用户的文件。注册时已执行 UUID 排他文件落盘；资源读取接口与会话删除后的附件清理策略尚未实现。

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

会话 ID、轮次 ID、轮次状态、创建时间和会话顺序已在数据库列中，不重复放入 payload。历史 payload 不保存 `supersedes_turn_id` 或 `superseded_by_turn_id`；通过会话逻辑顺序结合 `status=superseded` 表达用户方向调整，不按随机 UUID 排序。

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

本地 `tests/test_communication_store.py` 覆盖三表重开、用户隔离、最近摘要边界、过期摘要拒绝、工作区版本竞争、并发序号、非法内容回滚、外键和级联。测试文件沿用项目不追踪约定。
