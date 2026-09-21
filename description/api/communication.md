# 多轮对话 API

> 本文按接口组织。每个接口章节独立提供方法与完整路径、认证、参数、响应、错误码及请求示例；示例中的 login_code、c1、t1、文件路径须替换为实际值。

正式激活后已执行文件可读性、文件摘要、文件与文字业务相关性及文件文字整体判断，并将拒绝提示、任务终态同步至 SSE 与历史；上下文相关性及服务端历史选择已接入，完整门禁放行后进入正式 Agent Core，继续同一轮 SSE。

## 接口目录

| 接口 | 方法 | 完整路径 |
| --- | --- | --- |
| [获取当前用户会话列表](#获取当前用户会话列表) | `GET` | `/contract/api/communication/conversations` |
| [打开会话](#打开会话) | `POST` | `/contract/api/communication/conversations/{conversation_id}/open` |
| [向前刷新会话历史](#向前刷新会话历史) | `POST` | `/contract/api/communication/conversations/{conversation_id}/refresh` |
| [修改会话名称](#修改会话名称) | `PATCH` | `/contract/api/communication/conversations/{conversation_id}` |
| [删除会话](#删除会话) | `DELETE` | `/contract/api/communication/conversations/{conversation_id}` |
| [创建会话及首轮](#创建会话及首轮) | `POST` | `/contract/api/communication/conversations` |
| [创建或替换轮次](#创建或替换轮次) | `POST` | `/contract/api/communication/conversations/{conversation_id}/turns` |
| [订阅轮次事件流](#订阅轮次事件流) | `GET` | `/contract/api/communication/conversations/{conversation_id}/turns/{turn_id}/events` |
| [获取轮次快照](#获取轮次快照) | `GET` | `/contract/api/communication/conversations/{conversation_id}/turns/{turn_id}` |
| [取消轮次](#取消轮次) | `POST` | `/contract/api/communication/conversations/{conversation_id}/turns/{turn_id}/cancel` |

共用说明：[请求中的合同引用](#请求中的合同引用)。

接入顺序：创建会话及首轮 → 订阅激活 → 后续创建或替换轮次 → 订阅新轮次。刷新时读取快照，主动停止时调用取消接口。

业务错误通常为 `{"detail":"原因"}`；框架参数校验错误的 `detail` 为错误对象数组。OpenAPI 与 Schema 由 `app/router/communication.py`、`app/schema/communication.py` 提供。

---

## 获取当前用户会话列表

### 方法与用途

`GET /contract/api/communication/conversations`

返回当前登录用户在 SQLite 中保留的全部会话，用于会话列表展示。不依赖内存中是否仍存在轮次，不触发工作流。

请求头：`Authorization: Bearer <login_code>`。所有已登录用户均可查看自己的会话，登录方式见[审核用户登录 API](auth.md)。

### 请求参数

无路径参数、查询参数或请求体。用户归属由服务端从已认证用户密钥取得，客户端不提交用户标识或密钥。本接口暂不分页、不按任务状态筛选。

### 成功响应

`200 application/json`，直接返回 `ConversationListItem` 数组，没有会话时为 `[]`：

```json
[
  {
    "conversation_id": "a7f83b90-5ac2-4e16-8f92-71677c450dc1",
    "name": "采购合同分析",
    "created_at": 1788858000000
  }
]
```

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `conversation_id` | string | 会话 ID，可用于后续轮次提交。 |
| `name` | string | 当前会话名称，反映用户修改后的值。 |
| `created_at` | integer | 会话创建时间，UTC Unix 毫秒，可用 `new Date(created_at)` 展示。 |

按 `created_at` 降序排列，同一毫秒创建的会话按 `conversation_id` 降序确定稳定顺序。响应不包含密钥、轨迹、文件列表、工作区或内存轮次状态。

### 错误响应

| 状态码 | 条件 |
| --- | --- |
| 401 | 免登码无效或过期。 |
| 503 | 会话数据库暂时不可用；不返回内部数据库异常详情。 |

### 请求示例

```bash
curl 'http://127.0.0.1:20000/contract/api/communication/conversations' \
  -H 'Authorization: Bearer <login_code>'
```

### 行为与边界

即使某会话的任务已经结束、过期或因进程重启丢失，只要会话记录仍保留就会返回。本接口不承诺其历史轨迹已经备份，也不能用于恢复已丢失的内存任务。改名不改变创建时间或列表排序。

---

## 打开会话

### 方法与用途

`POST /contract/api/communication/conversations/{conversation_id}/open`

首次打开时读取最新摘要（包含）及其后的历史，加载到会话驻留缓存。没有摘要则加载全部已有记录。重复打开保留已向前加载的历史，并合并数据库中的新增尾部记录，不将范围重置为最新摘要。

请求头：`Authorization: Bearer <login_code>`。所有已登录用户均可打开自己的会话，缓存命中仍校验密钥归属。

### 请求参数

路径参数 conversation_id：必填 string，长度 1～128。无请求体、无查询参数。

### 成功响应

`200 application/json`，模型 ConversationTaskHistoryResponse，空会话示例：

```json
{
  "conversation_id": "c1",
  "name": "采购分析",
  "created_at": 1788858000000,
  "records": [],
  "has_more": false,
  "model_context_start_sequence": null
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `conversation_id` | string | 当前会话 ID。 |
| `name` | string | 当前会话名称。 |
| `created_at` | integer | 会话创建时间，UTC Unix 毫秒。 |
| `records` | array | 当前驻留范围内的全部任务，已剔除摘要；按原始 sequence 正序，序号可能不连续，不是仅本次新增片段。 |
| `has_more` | boolean | 内部驻留最早记录之前是否仍有历史；不按过滤后的任务列表计算。 |
| `model_context_start_sequence` | integer/null | 最新摘要序号；无摘要时为最早记录序号，无记录为 null。 |

每条 records 包含 `record_id/sequence/kind/turn_id/status/payload/created_at/activated_at/processing_duration_ms`，kind 固定为 task。摘要及内部工具轨迹不出现在响应中；用户恢复界面使用 `payload.events`，不再使用 `payload.trace`。不读取或返回 retrieval_text、embedding，不返回密钥或工作区。

`processing_duration_ms` 为任务从激活到终态的固定总处理时长（非负整数毫秒），例如 12500 表示 12.5 秒；未激活即结束为 0，处理中、旧记录或归档未提供计时为 null，前端应隐藏耗时或显示未知，不将 null 当作 0。activated_at 为首次激活 UTC Unix 毫秒，未激活或旧记录未知时为 null。任务状态可以为 pending_activation/processing；活跃半成品见 `payload.streaming_messages`，终态后半成品以 interrupted 消息进入 `payload.events`。

### 用户展示 Payload

| 字段 | 含义 |
| --- | --- |
| `input` | 用户原文 `text`、附件 `files` 与合同快照 `contracts`；新附件含 `file_id/file_name/display_name/summary/file_path/admission`，规则见下方。 |
| `events` | 完整精简展示记录，按原 SSE 顺序排列，保留所有 `turn.status/task.progress/message.completed/error` 业务事件。 |
| `streaming_messages` | 仍在生成的消息累积正文，含 `message_id/message_kind/text/status/references`，`status=streaming`；无活跃消息或任务结束时为 `[]`。 |
| `last_sequence` | 读取时已处理的最后 SSE 序号，包含过滤掉的 delta；旧历史未知时为 `null`。 |
| `event_source` | `recorded` 表示实际记录；`legacy` 表示从旧轨迹兼容恢复已有消息。 |

附件 `admission` 为 `pending`（待判断）、`accepted`（允许保存）、`unavailable`（不可用）。pending/unavailable 的 `file_path` 为 null，前端只展示名称，禁止打开；accepted 的路径为 `/{file_id}.pdf`，但异步备份完成前磁盘文件可能尚不存在。此时可通过[会话附件读取接口](resource.md#读取已驻留会话任务的-pdf-附件)按 `file_id` 读取内存文件；后端要求当前用户的对应任务轨迹已驻留且附件为 accepted，不自动加载旧任务。旧附件没有 admission 时兼容展示已有路径，但新资源接口不默认授予访问权。详细生命周期见[附件准入与延迟落盘](../architecture/system/communication-history.md#附件准入与延迟落盘)。

附件 `file_name` 始终为原始文件名；`display_name`、`summary` 为后端根据内容生成的名称和摘要，未生成时为 null，旧历史可缺省。前端可优先展示 `display_name`，缺少时回退至 `file_name`。这些字段随 open/refresh 返回的 `payload.input.files` 恢复，不新增 SSE 事件，也不接受前端提交或覆盖；摘要存在不代表附件可打开，仍以准入和资源授权为准。完整写入规则见[文件引用契约](../architecture/data/communication-sqlite.md#文件引用)。

单条展示事件示例：

```json
{
  "sequence": 12,
  "event": "message.completed",
  "data": {
    "turn_id": "t1",
    "message_id": "m1",
    "message_kind": "intermediate",
    "text": "目前已经查到两份合同，但尚未完成对比……",
    "status": "interrupted",
    "references": []
  }
}
```

`recorded` 记录的 `data` 与实时 SSE 完全一致，直接复用 [SSE 事件契约](#sse-事件契约)。`sequence` 保留原始序号，因过滤 delta 允许不连续；不存 delta、心跳或连接级错误，不重复播放打字过程。`message.completed` 按 `message_id` 覆盖正文和状态，不能再次拼接正文；`task.progress` 覆盖当前进度，遇任务终态停止动画。

恢复时先清空该轮旧展示并按顺序应用 `events`，再用 `streaming_messages` 覆盖对应消息的已生成正文。处理中任务使用 `last_sequence` 作为 `Last-Event-ID` 继续订阅，不能使用最后一个已保存事件的序号替代；游标超出缓存时仍按 SSE 契约读取快照恢复。已结束任务只恢复展示，不重新订阅或执行。

旧任务没有保存事件时，`event_source=legacy`：只按原消息首次出现顺序聚合已有正文，`sequence/last_sequence=null`，引用沿用旧 `type/location`；不伪造进度、错误、页码或 SSE 游标。生命周期以外层任务 `status` 为准。这是只读兼容投影，不重写旧 SQLite 数据。

model_context_start_sequence 仍表示内部模型窗口起点，可能指向未对外返回的摘要。records 为空不代表没有更早历史，前端应根据 has_more 决定是否继续刷新。

### 错误响应

| 状态码 | 条件 |
| --- | --- |
| 401 | 免登码无效或过期。 |
| 404 | 会话不存在或不属于当前用户。 |
| 409 | 历史无法通过加载结构校验，或持久化内容与已冻结驻留记录冲突。 |
| 422 | 路径参数不合法。 |
| 503 | 会话存储暂时不可用。 |

### 请求示例

```bash
curl -X POST 'http://127.0.0.1:20000/contract/api/communication/conversations/c1/open' \
  -H 'Authorization: Bearer <login_code>'
```

### 行为与边界

不创建或激活轮次，不调用模型，不生成摘要。返回统一驻留轨迹中的最新任务，包括待激活、处理中及已结束但未备份的任务；SQLite 用于补充历史，不覆盖 fresh 内容。任务进入终态后由后台独立备份，前端读取无需等待备份。不能用 SSE 快照冒充完整历史。

会话可能在空闲至少三十分钟且连续三次扫描无活动、归档全部成功后从内存驱逐。open 会重新从 SQLite 加载工作区及最新摘要窗口；不恢复已回收的实时 SSE 事件源。后台总结/向量化不改写原任务 payload，也不向此响应添加向量或加工标记。

驻留历史中的任务供前端查看，摘要仅内部保留；模型历史窗口始终为最新摘要及其后记录，不受向前刷新影响。已实现内部 get_model_records 方法执行该截取，尚未接入真实模型；权限后的状态过滤、token 预算及消息格式组装仍由后续 Agent 层完成。

---

## 向前刷新会话历史

### 方法与用途

`POST /contract/api/communication/conversations/{conversation_id}/refresh`

从当前驻留历史的最早记录向前扩展，遇到更早的一条摘要（包含）即停止；若前面没有摘要，则读取至会话起点。新旧记录按 sequence 组成完整顺序轨迹，不重复追加。

请求头：`Authorization: Bearer <login_code>`。所有已登录用户均可刷新自己的会话。

### 请求参数

路径参数 conversation_id：必填 string，长度 1～128。无请求体、查询参数或客户端游标；服务端以该会话当前驻留边界为准，须先调用 open。

### 成功响应

`200 application/json`，返回 ConversationTaskHistoryResponse，以下为驻留范围内仅有摘要时的示例（摘要不对外返回）：

```json
{
  "conversation_id": "c1",
  "name": "采购分析",
  "created_at": 1788858000000,
  "records": [],
  "has_more": true,
  "model_context_start_sequence": 2
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `conversation_id` | string | 当前会话 ID。 |
| `name` | string | 当前会话名称。 |
| `created_at` | integer | 会话创建时间，UTC Unix 毫秒。 |
| `records` | array | 当前驻留范围内的全部任务，已剔除摘要；按原始 sequence 正序，序号可能不连续，不是仅本次新增片段。 |
| `has_more` | boolean | 内部驻留最早记录之前是否仍有历史；不按过滤后的任务列表计算。 |
| `model_context_start_sequence` | integer/null | 最新摘要序号；无摘要时为最早记录序号，无记录为 null。 |

每条 records 包含 `record_id/sequence/kind/turn_id/status/payload/created_at/activated_at/processing_duration_ms`，kind 固定为 task。响应剔除摘要和内部工具轨迹，`payload` 包含 `input/events/streaming_messages/last_sequence/event_source`；结构、旧数据兼容与前端应用顺序见[用户展示 Payload](#用户展示-payload)。不读取或返回 retrieval_text、embedding，不返回密钥或工作区。

`processing_duration_ms` 为任务从激活到终态的固定总处理时长（非负整数毫秒），未知时为 null，未激活即结束为 0。activated_at 为首次激活 UTC Unix 毫秒，未激活或旧记录未知时为 null。活跃半成品通过 `payload.streaming_messages` 恢复；终态后以 interrupted 消息保存在 `payload.events`。

model_context_start_sequence 仍表示内部模型窗口起点，可能指向未对外返回的摘要。records 为空不代表没有更早历史，前端应根据 has_more 决定是否继续刷新。

例如初始驻留 [摘要6, 任务7]，一次刷新为 [摘要3, 任务4, 任务5, 摘要6, 任务7]；前端 records 从 [任务7] 变为 [任务4, 任务5, 任务7]。模型窗口仍为 [摘要6, 任务7]。无更早记录时返回相同任务范围及 has_more=false，不报错。

### 错误响应

| 状态码 | 条件 |
| --- | --- |
| 401 | 免登码无效或过期。 |
| 404 | 会话不存在或越权，优先于未驻留判断。 |
| 409 | 会话尚未 open、进程重启或空闲驱逐后驻留已清空，或历史记录冲突。 |
| 422 | 路径参数不合法。 |
| 503 | 会话存储暂时不可用。 |

### 请求示例

```bash
curl -X POST 'http://127.0.0.1:20000/contract/api/communication/conversations/c1/refresh' \
  -H 'Authorization: Bearer <login_code>'
```

### 行为与边界

返回完整驻留范围内的全部任务（剔除摘要），前端替换本地历史视图，不把返回列表再次整段追加。服务端串行处理并发刷新，每个请求从处理时的最新边界扩展，多个请求可能连续加载多段，但不会重复记录；前端建议等待上次刷新完成再发起下一次。

因空闲驱逐而返回 409 时，先调用 open 恢复驻留，再继续向前 refresh；已保存的会话及任务没有被删除。

刷新期间也合并已落库的新尾部记录。新摘要只更新模型窗口起点，不删除前端已加载的旧历史。失败不提交半段缓存；刷新不扩展模型可见历史、不修改工作区或数据库。

---

## 修改会话名称

### 方法与用途

`PATCH /contract/api/communication/conversations/{conversation_id}`

修改当前用户的会话名称。请求头：`Authorization: Bearer <login_code>`，所有已登录用户均可修改本人会话。

### 请求参数

| 位置 | 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| path | `conversation_id` | string | 是 | 长度 1～128，已存在且归属当前用户的会话 ID。 |
| JSON body | `name` | string | 是 | 原始长度 1～200，去除首尾空白后不得为空；禁止额外字段。 |

请求体媒体类型为 `application/json`，无查询参数：

```json
{"name": "采购合同分析"}
```

### 成功响应

`200 application/json`，返回修改后的 `ConversationListItem`：

```json
{
  "conversation_id": "c1",
  "name": "采购合同分析",
  "created_at": 1788858000000
}
```

`created_at` 仍为原会话创建时间（UTC Unix 毫秒），不因改名更新；不返回密钥。

### 错误响应

| 状态码 | 条件 |
| --- | --- |
| 401 | 免登码无效或过期。 |
| 404 | 会话不存在或不属于当前用户。 |
| 422 | 名称为空、纯空白、过长，或请求结构不合法。 |
| 503 | 会话数据库暂时不可用。 |

### 请求示例

```bash
curl -X PATCH 'http://127.0.0.1:20000/contract/api/communication/conversations/c1' \
  -H 'Authorization: Bearer <login_code>' \
  -H 'Content-Type: application/json' \
  -d '{"name":"采购合同分析"}'
```

### 行为与边界

只改变 SQLite 中的会话名称，不修改创建时间、归属、轨迹、工作区或运行轮次。列表重新读取后显示新名称，排序不变。

---

## 删除会话

### 方法与用途

`DELETE /contract/api/communication/conversations/{conversation_id}`

删除本人会话及 SQLite 中关联的全部任务、摘要和工作区记录。请求头：`Authorization: Bearer <login_code>`。所有已登录用户均可删除自己的会话。

### 请求参数

| 位置 | 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| path | `conversation_id` | string | 是 | 长度 1～128，已存在且归属当前用户的会话 ID。 |

无请求体、无查询参数。不接受密钥或用户标识字段。

### 成功响应

`204 No Content`，响应体为空，不返回 JSON。前端收到后将会话从列表移除。

### 错误响应

| 状态码 | 条件 |
| --- | --- |
| 401 | 免登码无效或过期。 |
| 404 | 会话不存在、不属于当前用户，或已被删除后再次请求。 |
| 422 | 路径参数不合法。 |
| 503 | 会话数据库暂时不可用；事务回滚，不部分删除关联条目。 |

### 请求示例

```bash
curl -i -X DELETE 'http://127.0.0.1:20000/contract/api/communication/conversations/c1' \
  -H 'Authorization: Bearer <login_code>'
```

### 行为与边界

物理删除 SQLite 会话，通过外键在同一事务级联删除记录表和工作区；不提供撤销接口，只有已有备份才能恢复。不会删除其他用户或其他会话的数据。

删除成功同步驱逐会话轨迹及该会话事件源，停止当前执行；数据库失败则保留内存。删除与后台备份串行，防止迟到备份或输出复活记录。既有 SSE 会收到 turn_unavailable 连接级错误后关闭，后续请求返回 404；不生成伪造的正常完成消息。不删除 upload 附件或正式合同/ES 数据。

---

## 请求中的合同引用

创建会话及提交新轮次均支持 `contract_ids`。前端只提交 ID，例如 `formData.append("contract_ids", documentId)`；多份合同重复追加字段，不提交 JSON 数组字符串。所有已登录用户可引用正式合同，已有会话仍须校验归属。

服务端在读取上传内容、创建会话、注册任务或替代原任务之前，先从合同 SQLite 查找全部引用，只接受 `ready` 合同，读取 `document_id`、`file_name`、`summary`。此过程不查询 ES、不读取 PDF，也不调用模型。任一引用无效则整次请求失败：ID 格式错误或数量超限返回 422，不存在或未就绪返回 404，数据库不可用返回 503；不创建新会话，不中断被替代任务。

读取结果保存在任务 `payload.input.contracts`，并随 open/refresh 展示历史返回，例如：

```json
{
  "document_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "file_name": "设备采购合同",
  "summary": "本合同涉及设备供应与安装服务。"
}
```

合同名称按库中值保留，不自动添加或删除 `.pdf`。旧合同摘要为 null 时仍可引用，上下文明确提示尚未保存摘要，不推测正文。任务保存当次读取快照，之后的元数据变更不改写历史；新请求重新读取。合同删除不删除历史快照，后续打开原文仍由文件工具检查实际资源。

引用合同不进入上传附件的可读性、摘要生成和主题冲突检查；已有摘要单独作为文件资料参与后续相关性判断，用户文字仍照常检查。完整门禁通过后，在用户输入下单独渲染“引用合同 N”，只包含合同 ID、文件名和摘要，不注入页数、展示名称或注意事项。完整 ID 可用于 `view_contract_file`。

合同快照随原任务持久化；记忆检索用户输入将合同与附件采用同样的文件区块编码机制，详见[检索文本模板](../architecture/workflow/conversation-memory/retrieval-embedding.md#用户输入存储模板)。不新增 SSE 事件。

---

## 创建会话及首轮

### 方法与用途

`POST /contract/api/communication/conversations`

创建 SQLite 会话及专属空工作区，同时注册首轮待激活任务。会话和轮次 ID 均由服务端生成，不要求前端先创建标识。

请求头：`Authorization: Bearer <login_code>`。登录方式见[审核用户登录 API](auth.md)。

### 请求参数

请求体：`multipart/form-data`；纯文字也支持表单提交，无路径或查询参数。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `name` | string | 否 | 最多 200 字符，去除首尾空白；未提供时使用北京时间 YYYY-MM-DD HH:mm:ss，纯空白名称拒绝。 |
| `text` | string | 否 | 最多 20000 字符，保留原文；纯空白视为无文字。 |
| `files` | PDF[] | 否 | 重复使用 files 字段；最多 10 份，每份 10 MiB，合计 20 MiB。 |
| `contract_ids` | string[] | 否 | 重复使用 contract_ids 表单字段，最多 10 项完整的 64 位小写 SHA-256；重复 ID 去重并保留首次出现顺序。 |

文字、上传文件和 `contract_ids` 至少一项非空。文件名去除目录部分后须以 `.pdf` 结尾且不超过 512 字符，内容不得为空。这里只做上传约束，不验证 PDF 可打开、加密、渲染或合同属性，不代表业务门禁已通过。

首轮不支持 `supersedes_turn_id`，会话归属从已认证用户的密钥取得，客户端不提交密钥或用户标识。

### 成功响应

`201 application/json`，模型 `TurnCreatedResponse`：

```json
{
  "conversation_id": "c1",
  "turn_id": "t1",
  "status": "pending_activation",
  "activation_expires_at": "2026-09-08T09:03:00Z",
  "supersedes_turn_id": null
}
```

| 字段 | 含义 |
| --- | --- |
| `conversation_id` | 会话标识，实际由服务端生成 UUID；示例简写为 c1。 |
| `turn_id` | 本轮标识，实际由服务端生成 UUID；示例简写为 t1。 |
| `status` | 固定为 `pending_activation`，尚未处理。 |
| `activation_expires_at` | 首次激活截止时间，UTC ISO 8601，注册后 180 秒。 |
| `supersedes_turn_id` | 本轮接替的旧轮次；普通新轮次或首轮为空。 |

当前响应不包含会话名称。成功只表示注册，前端须在截止时间前使用返回的两个 ID 订阅 SSE，才会激活处理。

### 错误响应

| 状态码 | 条件 |
| --- | --- |
| 400 | multipart 格式无法解析。 |
| 401 | 免登码无效或过期。 |
| 413 | PDF 数量、单文件大小或合计大小超限。 |
| 422 | 首次输入为空、文件不合法、名称或字段格式不合法。 |
| 503 | 内存容量已满或数据库暂时不可用。 |

### 请求示例

```bash
curl -X POST 'http://127.0.0.1:20000/contract/api/communication/conversations' \
  -H 'Authorization: Bearer <login_code>' \
  -F 'name=采购合同分析' \
  -F 'text=请查看这些合同' \
  -F 'files=@/path/to/contract.pdf'
```

仅提交文字时删除 files 行；使用默认名称时删除 name 行。

### 行为与边界

全部输入校验后才创建会话和空工作区；首轮注册失败会补偿删除本次新建且未产生内容的会话和工作区。数据库与内存不是跨进程事务，强制终止进程时不承诺跨资源原子性。请求暂不支持幂等重试，重复成功提交会创建不同会话。

会话及工作区持久化；首轮输入和有序轨迹立即驻留，附件仅分配 UUID 并暂存内存。只有明确通过准入的附件随终态备份保存至 upload；未通过或未判断就结束的附件仅保留不可用元数据，不阻塞轨迹复制至 SQLite。进程重启后已备份历史可通过 open 恢复展示，但旧 SSE 回放和执行不能恢复；未备份数据和附件在异常退出时可能丢失。

---

## 创建或替换轮次

### 方法与用途

`POST /contract/api/communication/conversations/{conversation_id}/turns`

在已有会话中提交后续问题，或用新问题替代执行中的旧轮次；不会隐式创建会话。

请求头：`Authorization: Bearer <login_code>`。登录方式见[审核用户登录 API](auth.md)。

### 请求参数

路径参数：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `conversation_id` | string | 是 | 长度 1～128；使用创建会话接口返回的 ID，必须存在并属于当前用户。 |

请求体：`multipart/form-data`；纯文字也支持表单提交。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `text` | string | 否 | 最多 20000 字符，保留原文；纯空白视为无文字。 |
| `files` | PDF[] | 否 | 重复使用 files 字段；最多 10 份，每份 10 MiB，合计 20 MiB。 |
| `contract_ids` | string[] | 否 | 重复使用 contract_ids 表单字段，最多 10 项完整的 64 位小写 SHA-256；重复 ID 去重并保留首次出现顺序。 |
| `supersedes_turn_id` | string | 否 | 长度 1～128；补充或调整时指定同会话中 `can_interrupt=true` 的处理中轮次。 |

文字、上传文件和 `contract_ids` 至少一项非空。文件名去除目录部分后须以 `.pdf` 结尾且不超过 512 字符，内容不得为空。这里只做上传约束，不验证 PDF 可打开、加密、渲染或合同属性，不代表业务门禁已通过。

本接口不接受会话命名操作。未指定替代 ID 时，同会话不得已有活跃轮次。

### 成功响应

`201 application/json`，模型 `TurnCreatedResponse`：

```json
{
  "conversation_id": "c1",
  "turn_id": "t1",
  "status": "pending_activation",
  "activation_expires_at": "2026-09-08T09:03:00Z",
  "supersedes_turn_id": null
}
```

| 字段 | 含义 |
| --- | --- |
| `conversation_id` | 会话标识，实际由服务端生成 UUID；示例简写为 c1。 |
| `turn_id` | 本轮标识，实际由服务端生成 UUID；示例简写为 t1。 |
| `status` | 固定为 `pending_activation`，尚未处理。 |
| `can_interrupt` | 正式工作流固定为 false；等待完整门禁通过后才允许取消或替代。 |
| `activation_expires_at` | 首次激活截止时间，UTC ISO 8601，注册后 180 秒。 |
| `supersedes_turn_id` | 本轮接替的旧轮次；普通新轮次或首轮为空。 |

替代时 `supersedes_turn_id` 返回提交的旧轮次 ID。新轮次仍待激活，前端须在截止时间前订阅其 SSE。

### 错误响应

| 状态码 | 条件 |
| --- | --- |
| 400 | multipart 格式无法解析。 |
| 401 | 免登码无效或过期。 |
| 404 | 会话或被替代轮次不存在、跨用户或跨会话。 |
| 409 | 已有活跃轮次但未正确指定替代、被替代轮次尚未开放中断，或被替代轮次已结束。 |
| 413 | PDF 数量或大小超限。 |
| 422 | 输入为空、文件不合法或字段格式错误。 |
| 503 | 内存容量已满或数据库暂时不可用。 |

### 请求示例

普通后续问题：

```bash
curl -X POST 'http://127.0.0.1:20000/contract/api/communication/conversations/c1/turns' \
  -H 'Authorization: Bearer <login_code>' \
  -F 'text=进一步分析付款条件'
```

调整执行中的方向：

```bash
curl -X POST 'http://127.0.0.1:20000/contract/api/communication/conversations/c1/turns' \
  -H 'Authorization: Bearer <login_code>' \
  -F 'text=先只分析验收条款' \
  -F 'supersedes_turn_id=t1'
```

### 行为与边界

输入校验或注册失败不替换旧轮次。替代成功后旧轮次变为 `superseded`，其 `context_status=user_goal_adjusted`，旧流关闭；新轮次仍待激活。新轮次过期不会恢复旧轮次。旧轮次已结束时直接创建普通新轮次，不改写旧终态。

不自动继承旧文字或文件，替代后旧运行时输入字节释放，但原文字和文件引用仍保留在同一会话的冻结轨迹中并后台备份。请求暂不具备幂等键；已有活跃轮次时重复提交会冲突。正式执行器只允许替代完整门禁通过后的任务，并停止旧后续执行协程、拒收迟到结果；门禁期间返回 `409`，旧任务继续执行且不注册新轮次。新轮门禁读取符合边界的近期历史；第二层上下文继承与旧文件原文复用尚未实现。

---

## 订阅轮次事件流

### 方法与用途

`GET /contract/api/communication/conversations/{conversation_id}/turns/{turn_id}/events`

首次有效订阅激活轮次，后续订阅用于回放和断线恢复。

请求头：`Authorization: Bearer <login_code>`。登录方式见[审核用户登录 API](auth.md)。

### 请求参数

无请求体、无查询参数。

| 位置 | 参数 | 必填 | 说明 |
| --- | --- | --- | --- |
| path | `conversation_id` | 是 | 长度 1～128；会话须存在且属于当前用户。 |
| path | `turn_id` | 是 | 同会话中已注册的轮次 ID。 |
| header | `Last-Event-ID` | 否 | 已接收事件的非负整数序号，最多 20 位；缺省为 0，仅返回更大序号。 |

### 成功响应与请求示例

`200 text/event-stream`，持续返回事件帧；不是普通 JSON 响应。

```bash
curl -N 'http://127.0.0.1:20000/contract/api/communication/conversations/c1/turns/t1/events' \
  -H 'Authorization: Bearer <login_code>' \
  -H 'Last-Event-ID: 0'
```

创建时快照序号为 0。首次订阅在 180 秒内完成身份与游标校验后，原子产生唯一的 `processing` 事件；无效游标不激活、不延期。重连不重跑任务，首次激活截止时间不限制已激活轮次的执行或重连。

### SSE 事件契约

订阅请求可以携带 `Last-Event-ID`，取值是该轮已接收的非负整数序号（最多 20 位），缺省为 `0`。日志事件从 `1` 起单调递增，仅发送大于游标的事件。

五类事件共用 `turn_id`，其余字段如下。事件数据由 `app/schema/communication.py` 定义，对外 SSE 封装由 `app/router/communication.py` 维护。

| 事件 | 专属字段 | 含义 |
| --- | --- | --- |
| `turn.status` | `created_at`、`status`、`can_interrupt`、`context_status`、`superseded_by_turn_id`、`activated_at`、`finished_at`、`processing_duration_ms` | `created_at` 为带时区的状态事件创建时间，其他字段含义同快照；仅 `superseded` 必须关联接替轮次，其余为空。 |
| `task.progress` | `type`、`message` | 当前用户展示状态；`type` 决定图标和默认文案，`message` 为简短业务说明，两者必填。不输出工具名、调用 ID、内部推理或伪造百分比。 |
| `message.delta` | `message_id`、`message_kind`、`delta` | 当前消息的文本增量；类型为 `intermediate/final`。 |
| `message.completed` | `message_id`、`message_kind`、`text`、`status`、`references` | 消息收束后的累积正文；`status=completed/interrupted`，分别表示完整输出和中断半成品。引用含 `document_id`、可空的 `page_number`（从 1 起）。 |
| `error` | `code`、`message`、`retryable` | 用户可见错误，不包含内部堆栈。 |

`task.progress`、`message.delta`、`message.completed` 和 `error` 不返回 `created_at` 或其他时间字段，`final` 消息同样不携带时间。前端通过事件 `id` 排序及回放，通过 `turn.status` 和快照的激活时间、结束时间与总处理时长展示任务计时，不再依赖中间事件时间。该变更不影响内部事件记录和历史存储，无需迁移数据。

`task.progress.type` 只允许以下三种值，不提供隐式默认值；`message` 为 1～2000 字符的非空文本，执行器应使用简短说明。前端按类型选择图标，并在不展示具体说明时使用下表默认文案，不应解析 `message` 推断类型。

| `type` | 默认文案 | 适用范围 |
| --- | --- | --- |
| `local-search` | 正在查阅资料 | 检索合同、读取上传文件、查阅历史记录。 |
| `online-search` | 正在联网检索 | 搜索网页、读取外部资料；保留给联网检索工具。 |
| `external-expert` | 正在咨询外部专家：本轮提问内容 | 发起专家咨询或在已有专家会话中追问。 |
| `thinking` | 正在思考 | 全部业务校验、理解问题、规划任务、计算分析、对比条款、组织答复。 |

新发布的 `task.progress.message` 统一采用“正在＋动作”（例如“正在咨询外部专家”）。工具注册时校验此前缀及非空动作，历史事件仍兼容旧文案。前端可以依据后续进度或轮次终态将上一条显示为“已结束：咨询外部专家”，但不能仅将“正在”替换成“已完成”来判定成功：工具失败后也会恢复思考，同一状态还可能因去重而不重复发送。本约束不适用于错误消息及用户回复正文。

前端须支持 `external-expert` 类型，优先展示后端 `message`，用 `type` 选择图标或样式；历史记录中的旧 `online-search` 不回写。

每次进度整体覆盖当前展示，不要求前端逐条堆积。多个内部工具可以共用一个展示状态，无需为每次调用发送开始、执行中和结束通知；内部工具轨迹仍独立保留。`type` 不是轮次生命周期状态，不改变 `processing/completed` 等状态；收到轮次终态后停止进度动画。该新增必填字段同步用于 SSE 和快照 `progress`，旧的只含 `message` 的发布方式不再有效。

```text
id: 2
event: task.progress
data: {"turn_id":"t1","type":"local-search","message":"正在查阅相关采购合同"}

```

业务门禁仍是内部独立子图，对外不设置专属事件或阶段状态。整个校验期间保持 `processing`，统一发送 `task.progress(type="thinking", message="正在思考")`，包括 PDF 打开、渲染和模型视觉判断，不使用 `local-search` 展示内部检查步骤。

#### 门禁拒绝与恢复

正式文件可读性、逐文件命名/摘要、文件与文字业务相关性及文件文字整体判断已接入首次订阅；纯文字、纯文件或文件加文字，在无历史时均已可聚合为 passed/rejected：拒绝按现有流式提示、rejected 终态与历史同步，通过时进入正式 Agent Core，装配驻留工作区、最新摘要、摘要后历史及当前任务，由 finish_task 提交最终答复并结束本轮。存在有效历史时，服务端从最新累计摘要之后选取最近最多五轮，排除当前轮及其后记录、拒绝、过期和非终态记录，再进行上下文关联判断；不向模型提供累计摘要，不跨摘要补足数量。上下文维度主要承接“继续”“还有补充吗”等依赖历史的请求；有文字时无历史也会判断，明确操作先前文件可判相关，文件定位、可用性核验及必要澄清留待后续服务，不代表文件已找到。只有文件且无历史时跳过该维度。前端无需提交历史或增加请求参数。仅在完整门禁通过后批准附件，保留真实名称、display_name、摘要和页数，随本轮终态后台备份；拒绝附件不落盘。门禁与 Agent Core 共用同一轮事件流和计时。正式门禁所有未放行情况（含判断或摘要执行失败）统一进入拒绝回复节点，并以 rejected 结束；回复明确区分服务故障与材料问题，不把技术失败解释为文件不合格。判断证据、理由、逐文件判断结果及私有审计不新增为公开 HTTP/SSE 或用户历史字段；已生成的 display_name/summary 仍按附件描述契约保留在用户输入中。

文字维度内部使用 related/uncertain/unrelated 三态，由聚合统一计分，详见[加权与阈值聚合](../architecture/workflow/contract-communication/business-gate.md#加权与阈值聚合)。uncertain 不新增为 SSE 或快照的轮次状态；纯文字无法确认业务主题时仍可能因总分不足形成 rejected，并流式提示补充问题。该调整不改变前端接口契约，不能将模型故障归入 uncertain。

上下文内部区分可见业务历史支持与仅引用先前文件，分别计分；明确 unrelated 的文字不能靠其他关联分放行。信息不足的简短追问仍允许上下文补足。依据字段不进入 SSE、快照或用户历史，无需前端新增字段，仍根据最终 rejected 状态展示流式指引。

- 文件校验不通过：`message.delta(message_kind=final)` → `message.completed(message_kind=final, status=completed)` → `turn.status(status=rejected)` → 关闭 SSE。
- 门禁中的模型服务不可用、结构化输出重试耗尽等已知检查故障，同样通过拒绝回复节点，以 `rejected` 结束，不再要求 `error(code=business_gate_failed)`。回复会说明服务或处理暂不可用，而非认定用户文件不合格。未捕获的程序异常、生命周期失败以及后续问答故障仍可产生 `failed`，前端不能只靠 error 事件判断结束。
- **前端以 `turn.status.status` 识别拒绝**；`message.completed.status=completed` 只表示提示正文完整，不表示业务成功。恢复时读取任务/快照 `status=rejected`，展示历史 `payload.events` 中同一条最终消息。
- 最后一条完整消息和终态在同一临界区同步写入快照、SSE 日志和驻留历史，终态触发后台 SQLite 备份；不等待落盘才展示。中间 delta 不落库，完整拒绝正文与终态落库，刷新或重新加载后保持一致。
- 拒绝后的本轮全部附件释放内存字节，历史保留 `admission=unavailable, file_path=null` 元数据，不落盘。前端应禁用文件查看。
- 门禁及拒绝回复生成、流式展示期间 `can_interrupt=false`，用户取消或调整方向返回 `409`；系统关闭、会话删除等生命周期清理仍可停止执行，半成品按原规则收束。

提示正文由专门模型根据业务日志生成自然语言；生成完整 JSON 并校验后，只将回复正文按片段发送，不透传证据、推理、JSON 或纠错记录。以下仅为展示示例，不是固定标题或文案契约：

```markdown
第2份文件「合同.pdf」的第3页主要文字模糊，请提供清晰版本。

本轮尚未开展业务分析。本轮上传的全部文件均不会保存为可用历史附件；请在下一次提交时一并上传需要处理的全部文件。
```

正常回复由模型统一组织原因、处理停止及重新提交范围，程序不再追加固定说明；回复模型故障或有限纠错耗尽时使用完整兜底文案，仍保持 rejected。仅兜底文案由程序补齐本轮未开展业务分析及全部所需附件的重新提交范围；不暗示其他文件已处理，无附件时不添加上传要求。正文中的 Markdown/HTML 按纯文本转义，前端仍需执行常规安全渲染。内部原始检查状态、日志和审计不新增为接口字段；设计见[统一拒绝与响应](../architecture/workflow/contract-communication/business-gate.md#统一拒绝与响应)。

兼容性变更：已移除旧 `gate.result` 事件和快照 `gate_result` 字段，不保留旧协议兼容分支。前端应移除专属监听、卡片及字段依赖，统一消费进度和消息；历史 Payload 与 SQLite 表结构不变，无需迁移历史数据。

标准帧示例：

```text
id: 2
event: message.delta
data: {"turn_id":"t1","message_id":"m1","message_kind":"intermediate","delta":"已完成文件检查。"}

```

`task.progress` 可以穿插在同一消息的多个 `message.delta` 之间。消息只用于有价值的阶段结论、澄清问题或最终答复，不逐步播报内部思考或每个工具操作；没有工具调用时也可以直接输出消息。每条消息使用独立 `message_id`，同时仅允许一条消息处于生成中。

`message_kind` 是必填字段，同一 `message_id` 的全部增量、完成事件和快照保持一致，不提供默认类型：

- `intermediate`：执行中的阶段提示。
- `final`：本轮最终答复，可以是总结，也可以是澄清或确认请求，不表示用户的整体目标已经完成。

`message.completed` 表示消息已收束，不保证正文完整，必须读取 `status`。正常发布默认 `completed`；`interrupted` 由运行时在任务中断或流式输出校验失败时生成；后者可以继续当前任务纠错。其 `text` 必须等于已发送增量拼接结果；正常消息也支持没有增量而直接提交全文。前端按 ID 覆盖正文，不能将它追加到已有 delta 后。已收束消息不能再次追加，消息类型不能中途改变。

取消、替代、失败或拒绝时，若有正在生成的消息，运行时先发送 `message.completed(status=interrupted)`，再发送 `turn.status` 终态；两者与内存历史原子提交，正常最终答复仍要求 `status=completed`。没有半成品时不虚构空消息。系统关闭和运行时 TTL 失败冻结也按同一规则保存历史，但连接已失效时不保证客户端收到末尾事件。

正常交付顺序为：`final` 增量（可选）→ `message.completed`（`final`）→ `turn.status`（`completed`）→ 关闭流。轮次标记 `completed` 前必须已完成唯一一条 `final` 消息；最终答复完成后仅接受轮次终态，不允许再发阶段进度、追加消息或发布第二条最终答复。取消、拒绝和失败无需强制生成 `final`。

`completed/cancelled/superseded/rejected/failed/expired` 为轮次终态，终态后拒绝发布迟到事件。已激活轮次的终态发送后关闭流；已收到其终态序号的重连立即结束。未激活过期记录订阅直接返回 `410`，不建立 SSE。取消、被替代、拒绝或失败可以中断正在生成的消息，快照将其标为 `interrupted`。`error` 本身不结束轮次，业务失败仍需另行发布 `turn.status: failed`。

不再使用 `awaiting_confirmation` 轮次状态，该旧值会被 Schema 拒绝。需要用户澄清或文件剔除确认时，以 `final` 提出问题，正常完成本轮并关闭流。用户回复通过同一 `conversation_id` 下的新轮次进入，获得新 `turn_id` 和新事件流；“待用户确认”属于会话或任务上下文，不维持旧 SSE。创建及门禁的轻量历史关联、第二层上下文继承均已实现；新轮次读取驻留工作区、最新摘要及其后历史，当前任务继续使用原生工具调用轨迹。

处理期间收到用户补充或方向调整时，旧轮次使用 `superseded`（已被替代）而不是 `cancelled`（用户手动终止），并在 `superseded_by_turn_id` 中指向新轮次。新轮次保持同一会话，独立从事件序号 1 开始。旧轮次若已完成则保留原终态，直接创建普通新轮次，不改写历史。

上述替代通过 HTTP 创建接口接入内部原子约束，并取消旧轮次生产协程；迟到结果不能写回已结束任务。已发往远端的模型请求停止计算与否仍取决于服务端取消能力。

### 错误响应、回放与心跳

- 先提交事件和快照，再供订阅者读取；回放与实时输出按同一轮次序号连续推进。
- 空闲时默认每 15 秒发送 `: heartbeat` 注释，不占序号，不作为第七类业务事件。
- 响应含 `Cache-Control: no-cache, no-transform` 和 `X-Accel-Buffering: no`。代理也须允许长连接并禁用缓冲；浏览器可使用带 Bearer 头的流式 `fetch`。
- 断开 SSE 不取消轮次，重连不重跑业务；前端按事件序号去重。
- 建连前：认证错误 `401`，轮次不可见 `404`，游标落后于缓存或超过最新事件 `409`，激活超时 `410`，游标格式错误 `422`，均为 JSON 错误。
- 已建连后：慢客户端落后于缓存时发送无 `id` 的 `error`，`code=replay_required`、`retryable=true`，然后关闭连接。轮次过期或服务关闭使用 `code=turn_unavailable`、`retryable=false`。这些连接级错误不写轮次日志、不改变轮次状态，也不包含 `created_at`。
- 收到回放缺口后，先读取快照替换本地展示状态，再以快照 `last_sequence` 重新订阅；若再次超出范围，重新执行恢复。

订阅示例（事件源须先通过创建接口或内部服务注册）：

```http
GET /contract/api/communication/conversations/c1/turns/t1/events
Authorization: Bearer <login_code>
Last-Event-ID: 0
```

会话数据库暂时不可用时，建连前返回 `503 application/json`。返回 200 后不得依赖 HTTP 状态判断业务是否完成，应监听事件终态。

---

## 获取轮次快照

### 方法与用途

`GET /contract/api/communication/conversations/{conversation_id}/turns/{turn_id}`

获取当前轮次展示状态，用于刷新、计时和断线恢复；读取不会激活任务。

请求头：`Authorization: Bearer <login_code>`。登录方式见[审核用户登录 API](auth.md)。

### 请求参数

无请求体、无查询参数。

| 参数 | 位置 | 必填 | 说明 |
| --- | --- | --- | --- |
| `conversation_id` | path | 是 | 长度 1～128，已存在且属于当前用户的会话。 |
| `turn_id` | path | 是 | 同会话中的轮次 ID。 |

### 成功响应

`200 application/json`，模型 `CommunicationSnapshot`。未激活轮次示例：

```json
{
  "conversation_id": "c1",
  "turn_id": "t1",
  "status": "pending_activation",
  "activated_at": null,
  "finished_at": null,
  "processing_duration_ms": null,
  "context_status": null,
  "activation_expires_at": "2026-09-08T09:03:00Z",
  "supersedes_turn_id": null,
  "superseded_by_turn_id": null,
  "last_sequence": 0,
  "earliest_sequence": 1,
  "messages": [],
  "progress": null,
  "error": null
}
```

### 展示快照

`can_interrupt` 为服务端控制的 boolean：待激活、完整业务门禁判断及拒绝提示输出期间均为 false；门禁通过并进入后续执行时为 true；所有终态均为 false。取消按钮和通过补充输入替代当前轮次的入口都应据此启用，不能从 `processing` 或“正在思考”推断权限。收到 `409` 时保留当前流，等待状态更新。

权限开放会追加一次 `turn.status(status=processing, can_interrupt=true)`，即同一轮可收到两次 processing 状态，前端不能按状态值去重；按事件序号处理即可。此次更新保留原 `activated_at`，不重启计时。创建响应和 open/refresh 返回的每个任务条目顶层也包含 `can_interrupt`；刷新时直接采用服务端当前值，不用从轨迹猜测。旧客户端数据缺少字段时按 false 处理。

`GET /conversations/{conversation_id}/turns/{turn_id}` 返回 `CommunicationSnapshot`，包括：

- `conversation_id`、`turn_id`、当前 `status`。
- `context_status`：只读派生标记；被替代为 `user_goal_adjusted`（用户目标调整），手动取消为 `user_manually_stopped`（用户手动终止），其他状态为 `null`。`turn.status` 事件同样携带，供后续上下文构造使用。
- `activation_expires_at`：首次激活截止时间；`activated_at`：首次成功激活时间，未激活为 `null`。这些时间不因重连变化。
- `finished_at`：进入任意终态的 UTC 时间，未结束为 `null`。
- `processing_duration_ms`：终态固定的总处理时长（整数毫秒），未结束为 `null`。从首次激活到终态计时，包含运行期间的等待和断线时间，不包含创建后等待订阅的时间；未激活就取消、被替代或过期时为 `0`。
- `supersedes_turn_id`：本轮因调整接替的旧轮次；`superseded_by_turn_id`：接替本轮的新轮次。没有关联时为 `null`。
- `last_sequence`：快照对应的最新事件序号；`earliest_sequence`：缓存最早可用事件序号。
- `messages`：按生成顺序排列，每条含 `message_id/message_kind/text/status/references`，状态为 `streaming/completed/interrupted`。
- `progress`、`error`：各自最近一次事件负载，没有则为 `null`，负载保留 `event_type` 标签。`progress` 包含 `type` 和 `message`，只保存最后一条，不是进度列表；终态仍保留供恢复，但不再表示正在执行。校验结论作为普通消息保存在 `messages`，不另设专属字段。

快照和事件在同一锁内更新，因此快照序号与消息内容一致。快照是用户界面恢复数据，不是模型上下文；取消后展示中的中断文本不代表会继续送给模型。上下文终止语义仍待对话运行时实现。

前端处理中可使用 `Math.max(0, Date.now() - Date.parse(activated_at))` 展示实时毫秒数（客户端时钟偏差会影响该估算）；收到终态后停止计时，改用服务端 `processing_duration_ms`。三个时间字段同时包含在 `turn.status` 中，无需为终态耗时额外请求快照。时间戳采用带时区的 ISO 8601 UTC 格式，总耗时由服务端单调时钟计算，不受系统校时影响。重连、回放、重复取消与快照读取都不会重置或延长已记录的耗时。

状态枚举：`pending_activation/processing/completed/cancelled/superseded/rejected/failed/expired`。末六种为终态。

### 错误响应

| 状态码 | 条件 |
| --- | --- |
| 401 | 免登码无效或过期。 |
| 404 | 会话或轮次不存在、越权、轮次已清理或因进程重启丢失。 |
| 422 | 路径参数格式不合法。 |
| 503 | 会话数据库暂时不可用。 |

保留期内的过期轮次返回 `200` 和 `status=expired`，不是订阅接口的 `410`。

### 请求示例

```bash
curl 'http://127.0.0.1:20000/contract/api/communication/conversations/c1/turns/t1' \
  -H 'Authorization: Bearer <login_code>'
```

---

## 取消轮次

### 方法与用途

`POST /contract/api/communication/conversations/{conversation_id}/turns/{turn_id}/cancel`

主动停止当前轮次，释放暂存输入并向已连接 SSE 发布终态。

请求头：`Authorization: Bearer <login_code>`。登录方式见[审核用户登录 API](auth.md)。

### 请求参数

无请求体、无查询参数。

| 参数 | 位置 | 必填 | 说明 |
| --- | --- | --- | --- |
| `conversation_id` | path | 是 | 长度 1～128，已存在且属于当前用户的会话。 |
| `turn_id` | path | 是 | 同会话中要取消的轮次 ID。 |

### 成功响应

`200 application/json`，模型 `CommunicationSnapshot`。仅已开放中断的任务可以首次取消，下面示例省略消息内容：

```json
{
  "conversation_id": "c1",
  "turn_id": "t1",
  "status": "cancelled",
  "can_interrupt": false,
  "activated_at": "2026-09-08T09:00:00Z",
  "finished_at": "2026-09-08T09:01:00Z",
  "processing_duration_ms": 60000,
  "context_status": "user_manually_stopped",
  "activation_expires_at": "2026-09-08T09:03:00Z",
  "supersedes_turn_id": null,
  "superseded_by_turn_id": null,
  "last_sequence": 1,
  "earliest_sequence": 1,
  "messages": [],
  "progress": null,
  "error": null
}
```

关键结果：`status=cancelled`、`context_status=user_manually_stopped`，填写 `finished_at` 和固定 `processing_duration_ms`。正在生成的消息变为 `interrupted`，完整字段定义见[展示快照](#展示快照)。

### 错误响应

| 状态码 | 条件 |
| --- | --- |
| 401 | 免登码无效或过期。 |
| 404 | 会话或轮次不存在、跨用户、跨会话或已清理。 |
| 409 | `can_interrupt=false` 的非终态任务，或已处于 completed、superseded、rejected、failed、expired 终态。 |
| 422 | 路径参数格式不合法。 |
| 503 | 会话数据库暂时不可用。 |

### 请求示例

```bash
curl -X POST 'http://127.0.0.1:20000/contract/api/communication/conversations/c1/turns/t1/cancel' \
  -H 'Authorization: Bearer <login_code>'
```

### 状态转换与边界

`POST /conversations/{conversation_id}/turns/{turn_id}/cancel` 不需要请求体，使用当前登录用户身份，成功返回 `200 application/json`，响应模型与 GET 快照相同。

| 当前状态 | 行为 |
| --- | --- |
| `pending_activation` | 返回 `409`，等待订阅激活或自动过期。 |
| `processing` 且 `can_interrupt=false` | 返回 `409`，旧任务继续执行，不改变状态、文件或轨迹。 |
| `processing` 且 `can_interrupt=true` | 进入 `cancelled`，释放输入，活跃消息标为 `interrupted`；已连接 SSE 收到终态后关闭。 |
| `cancelled` | 幂等返回相同快照，不追加事件、不刷新保留时间。 |
| 其他终态 | 返回 `409`，不覆盖完成、替代、拒绝、失败或过期记录。 |
| 不存在或跨用户、跨会话 | 返回 `404`，不修改任何状态。 |

认证失败返回 `401`。激活超时已经生效时，取消按 `expired` 返回 `409`；保留期结束后返回 `404`。中断权限更新与取消、替代、完成共用锁：权限尚未开放时拒绝用户操作；开放后与其他终态竞争时先提交者生效。终态后禁止旧事件继续发布。已取消任务重复取消仍幂等返回原快照。

```http
POST /contract/api/communication/conversations/c1/turns/t1/cancel
Authorization: Bearer <login_code>
```

取消不删除正式合同或回滚工具副作用。当前输入暂存会释放，但展示快照可保留标记中断的文本；它不是后续模型上下文。用户原问题和“已终止”标记的长期保留须在历史存储与上下文层实现，不能据本接口推断已完成。

---

## 实现边界与相关文档

会话和空工作区已持久化，任务轨迹收集、历史查询、摘要生成、上下文继承、向量化及真实模型/工具执行仍待接入。本文不把设计中的能力列为可调用接口。

- [事件运行时](../architecture/system/communication-events.md)：内部事件提交、回放与容量限制。
- [SQLite 存储](../architecture/data/communication-sqlite.md)：三表、密钥归属与存储配置。
- [用户上下文设计](../architecture/workflow/contract-communication/user-context.md)：取消与调整的模型可见语义。


## Agent Core 输出增量

`emit_progress` 和 `finish_task` 已通过 `message.delta` 展示生成中的正文，并在工具校验和执行成功后发送 `message.completed`。预览失败时会收到 `message.completed(status=interrupted)`，此时任务可能继续；前端不能仅因 final 类型消息出现就关闭订阅，仍以 `turn.status` 终态为准。推理和其他工具参数不作为正文推送。详见[交互工具增量展示](../architecture/workflow/contract-communication/interaction-tools.md#增量展示)。


## 工具执行状态展示

工具状态通过既有 `task.progress` 发送，type 和 message 来自程序注册配置。未覆盖配置的工具使用 thinking / 正在思考；查找和联网检索可分别使用 local-search、online-search，专家咨询使用 external-expert。实际执行前切换，结束后恢复 thinking，相同状态不重复推送。最终正文完成或任务终态后不再恢复状态。此类事件仅表示当前步骤，不包含工具参数或结果；用户正文仍只由两种交互工具发送。
