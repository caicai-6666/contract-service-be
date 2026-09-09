# 多轮对话 API

> 本文按接口组织。每个接口章节独立提供方法与完整路径、认证、参数、响应、错误码及请求示例；示例中的 login_code、c1、t1、文件路径须替换为实际值。

真实分析工作流尚未接入；默认激活后只有状态和心跳。启用 `COMMUNICATION_DEMO_ENABLED=true` 可通过同一组接口测试模拟输出，见[样式联调说明](../capability/application/communication-ui-demo.md)。

## 接口目录

| 接口 | 方法与完整路径 |
| --- | --- |
| [获取当前用户会话列表](#获取当前用户会话列表) | GET /contract/api/communication/conversations |
| [打开会话](#打开会话) | POST /contract/api/communication/conversations/{conversation_id}/open |
| [向前刷新会话历史](#向前刷新会话历史) | POST /contract/api/communication/conversations/{conversation_id}/refresh |
| [修改会话名称](#修改会话名称) | PATCH /contract/api/communication/conversations/{conversation_id} |
| [删除会话](#删除会话) | DELETE /contract/api/communication/conversations/{conversation_id} |
| [创建会话及首轮](#创建会话及首轮) | POST /contract/api/communication/conversations |
| [创建或替换轮次](#创建或替换轮次) | POST /contract/api/communication/conversations/{conversation_id}/turns |
| [订阅轮次事件流](#订阅轮次事件流) | GET /contract/api/communication/conversations/{conversation_id}/turns/{turn_id}/events |
| [获取轮次快照](#获取轮次快照) | GET /contract/api/communication/conversations/{conversation_id}/turns/{turn_id} |
| [取消轮次](#取消轮次) | POST /contract/api/communication/conversations/{conversation_id}/turns/{turn_id}/cancel |

接入顺序：创建会话及首轮 → 订阅激活 → 后续创建或替换轮次 → 订阅新轮次。刷新时读取快照，主动停止时调用取消接口。

业务错误通常为 `{"detail":"原因"}`；框架参数校验错误的 `detail` 为错误对象数组。OpenAPI 与 Schema 由 `app/router/communication.py`、`app/schema/communication.py` 提供。

---

## 获取当前用户会话列表

### 方法与用途

`GET /contract/api/communication/conversations`

返回当前登录用户在 SQLite 中保留的全部会话，用于会话列表展示。不依赖内存中是否仍存在轮次，不触发工作流。

请求头：`Authorization: Bearer <login_code>`。所有权限等级均可查看自己的会话，登录方式见[审核用户登录 API](auth.md)。

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

请求头：`Authorization: Bearer <login_code>`。所有等级均可打开自己的会话，缓存命中仍校验密钥归属。

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

每条 records 包含 `record_id/sequence/kind/turn_id/status/payload/created_at/activated_at/processing_duration_ms`，kind 固定为 task。摘要仅保留在后端内存，不出现在响应记录中。任务内轨迹位于 payload.trace，字段详见 [Payload 契约](../architecture/data/communication-sqlite.md#payload-契约)。不读取或返回 retrieval_text、embedding，不返回密钥或工作区。

`processing_duration_ms` 为任务从激活到终态的固定总处理时长（非负整数毫秒），例如 12500 表示 12.5 秒；未激活即结束为 0，处理中、旧记录或归档未提供计时为 null，前端应隐藏耗时或显示未知，不将 null 当作 0。不增加中间轨迹时间戳。activated_at 为首次激活 UTC Unix 毫秒，未激活或旧记录未知时为 null；前端可用它显示运行时长。实时任务 status 可为 pending_activation/processing，trace 消息可为 streaming；终态后固定耗时并将未完成片段标为 interrupted。

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

请求头：`Authorization: Bearer <login_code>`。所有等级均可刷新自己的会话。

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

每条 records 包含 `record_id/sequence/kind/turn_id/status/payload/created_at/activated_at/processing_duration_ms`，kind 固定为 task。摘要仅保留在后端内存，不出现在响应记录中。任务内轨迹位于 payload.trace，字段详见 [Payload 契约](../architecture/data/communication-sqlite.md#payload-契约)。不读取或返回 retrieval_text、embedding，不返回密钥或工作区。

`processing_duration_ms` 为任务从激活到终态的固定总处理时长（非负整数毫秒），例如 12500 表示 12.5 秒；未激活即结束为 0，处理中、旧记录或归档未提供计时为 null，前端应隐藏耗时或显示未知，不将 null 当作 0。不增加中间轨迹时间戳。activated_at 为首次激活 UTC Unix 毫秒，未激活或旧记录未知时为 null；前端可用它显示运行时长。实时任务 status 可为 pending_activation/processing，trace 消息可为 streaming；终态后固定耗时并将未完成片段标为 interrupted。

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

修改当前用户的会话名称。请求头：`Authorization: Bearer <login_code>`，所有权限等级均可修改本人会话。

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

删除本人会话及 SQLite 中关联的全部任务、摘要和工作区记录。请求头：`Authorization: Bearer <login_code>`。所有权限等级均可删除自己的会话，不套用正式合同删除的 1 级权限。

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

删除成功同步驱逐会话轨迹及该会话事件源，停止演示执行；数据库失败则保留内存。删除与后台备份串行，防止迟到备份或输出复活记录。既有 SSE 会收到 turn_unavailable 连接级错误后关闭，后续请求返回 404；不生成伪造的正常完成消息。不删除 upload 附件或正式合同/ES 数据。

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

文字和文件至少一项非空。文件名去除目录部分后须以 `.pdf` 结尾且不超过 512 字符，内容不得为空。这里只做上传约束，不验证 PDF 可打开、加密、渲染或合同属性，不代表业务门禁已通过。

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

会话及工作区持久化；首轮输入和有序轨迹立即驻留，附件以 UUID 文件保存到 upload。终态轨迹后台复制到 SQLite。进程重启后已备份历史可通过 open 恢复展示，但旧 SSE 回放和执行不能恢复；未备份数据在异常退出时可能丢失。

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
| `supersedes_turn_id` | string | 否 | 长度 1～128；补充或调整时指定同会话中需要替代的待激活/处理中轮次。 |

文字和文件至少一项非空。文件名去除目录部分后须以 `.pdf` 结尾且不超过 512 字符，内容不得为空。这里只做上传约束，不验证 PDF 可打开、加密、渲染或合同属性，不代表业务门禁已通过。

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
| `activation_expires_at` | 首次激活截止时间，UTC ISO 8601，注册后 180 秒。 |
| `supersedes_turn_id` | 本轮接替的旧轮次；普通新轮次或首轮为空。 |

替代时 `supersedes_turn_id` 返回提交的旧轮次 ID。新轮次仍待激活，前端须在截止时间前订阅其 SSE。

### 错误响应

| 状态码 | 条件 |
| --- | --- |
| 400 | multipart 格式无法解析。 |
| 401 | 免登码无效或过期。 |
| 404 | 会话或被替代轮次不存在、跨用户或跨会话。 |
| 409 | 已有活跃轮次但未正确指定替代，或被替代轮次已结束。 |
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

不自动继承旧文字或文件，替代后旧运行时输入字节释放，但原文字和文件引用仍保留在同一会话的冻结轨迹中并后台备份。请求暂不具备幂等键；已有活跃轮次时重复提交会冲突。真实工作流中断与模型上下文继承尚未实现，关闭旧 SSE 不等于已停止真实模型调用。

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

五类事件共用 `turn_id` 和带时区的 `created_at`，其余字段如下。字段定义由 `app/schema/communication.py` 维护。

| 事件 | 专属字段 | 含义 |
| --- | --- | --- |
| `turn.status` | `status`、`context_status`、`superseded_by_turn_id`、`activated_at`、`finished_at`、`processing_duration_ms` | 状态与时间字段含义同快照；仅 `superseded` 必须关联接替轮次，其余为空。 |
| `task.progress` | `message` | 用户可见进度，不输出内部推理或伪造百分比。 |
| `message.delta` | `message_id`、`message_kind`、`delta` | 当前消息的文本增量；类型为 `intermediate/final`。 |
| `message.completed` | `message_id`、`message_kind`、`text`、`references` | 完整文本、消息类型和引用；引用含 `document_id`、可空的 `page_number`（从 1 起）。 |
| `error` | `code`、`message`、`retryable` | 用户可见错误，不包含内部堆栈。 |

业务门禁仍是内部独立子图，对外不设置专属事件或阶段状态。校验期间保持 `processing`，通过 `task.progress` 提示“正在校验问题”或“正在校验上传文件”；检查结论使用普通 `message.delta/message.completed` 输出，涉及文件剔除时说明文件名称、保留或剔除结果及原因。结果消息进入快照与历史轨迹，不只保存在临时进度中。需要确认时以 `final` 提问并结束为 `completed`；明确拒绝继续时输出说明并结束为 `rejected`。实际校验子图仍未接入，当前展示执行器遵循此协议。

兼容性变更：已移除旧 `gate.result` 事件和快照 `gate_result` 字段，不保留旧协议兼容分支。前端应移除专属监听、卡片及字段依赖，统一消费进度和消息；历史 Payload 与 SQLite 表结构不变，无需迁移历史数据。

标准帧示例：

```text
id: 2
event: message.delta
data: {"turn_id":"t1","created_at":"2026-09-08T09:00:00+00:00","message_id":"m1","message_kind":"intermediate","delta":"已完成文件检查。"}

```

`task.progress` 可以穿插在同一消息的多个 `message.delta` 之间。一轮可以先输出阶段提示、再输出澄清问题或分析结论；每条消息使用独立 `message_id`，同时仅允许一条消息处于生成中。

`message_kind` 是必填字段，同一 `message_id` 的全部增量、完成事件和快照保持一致，不提供默认类型：

- `intermediate`：执行中的阶段提示。
- `final`：本轮最终答复，可以是总结，也可以是澄清或确认请求，不表示用户的整体目标已经完成。

`message.completed` 只完成当前消息，不自动结束轮次。有增量时，其 `text` 必须等于该消息全部增量拼接结果；也支持直接发送一条完整消息。已完成消息不能再次追加，消息类型不能中途改变。

正常交付顺序为：`final` 增量（可选）→ `message.completed`（`final`）→ `turn.status`（`completed`）→ 关闭流。轮次标记 `completed` 前必须已完成唯一一条 `final` 消息；最终答复完成后仅接受轮次终态，不允许再发阶段进度、追加消息或发布第二条最终答复。取消、拒绝和失败无需强制生成 `final`。

`completed/cancelled/superseded/rejected/failed/expired` 为轮次终态，终态后拒绝发布迟到事件。已激活轮次的终态发送后关闭流；已收到其终态序号的重连立即结束。未激活过期记录订阅直接返回 `410`，不建立 SSE。取消、被替代、拒绝或失败可以中断正在生成的消息，快照将其标为 `interrupted`。`error` 本身不结束轮次，业务失败仍需另行发布 `turn.status: failed`。

不再使用 `awaiting_confirmation` 轮次状态，该旧值会被 Schema 拒绝。需要用户澄清或文件剔除确认时，以 `final` 提出问题，正常完成本轮并关闭流。用户回复通过同一 `conversation_id` 下的新轮次进入，获得新 `turn_id` 和新事件流；“待用户确认”属于会话或任务上下文，不维持旧 SSE。创建已实现，上下文继承尚未实现。

处理期间收到用户补充或方向调整时，旧轮次使用 `superseded`（已被替代）而不是 `cancelled`（用户手动终止），并在 `superseded_by_turn_id` 中指向新轮次。新轮次保持同一会话，独立从事件序号 1 开始。旧轮次若已完成则保留原终态，直接创建普通新轮次，不改写历史。

上述替代已通过 HTTP 创建接口接入内部原子约束，但尚未执行真实任务中断。不能把“已关闭旧 SSE”当成已经终止后台模型调用。

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

`GET /conversations/{conversation_id}/turns/{turn_id}` 返回 `CommunicationSnapshot`，包括：

- `conversation_id`、`turn_id`、当前 `status`。
- `context_status`：只读派生标记；被替代为 `user_goal_adjusted`（用户目标调整），手动取消为 `user_manually_stopped`（用户手动终止），其他状态为 `null`。`turn.status` 事件同样携带，供后续上下文构造使用。
- `activation_expires_at`：首次激活截止时间；`activated_at`：首次成功激活时间，未激活为 `null`。这些时间不因重连变化。
- `finished_at`：进入任意终态的 UTC 时间，未结束为 `null`。
- `processing_duration_ms`：终态固定的总处理时长（整数毫秒），未结束为 `null`。从首次激活到终态计时，包含运行期间的等待和断线时间，不包含创建后等待订阅的时间；未激活就取消、被替代或过期时为 `0`。
- `supersedes_turn_id`：本轮因调整接替的旧轮次；`superseded_by_turn_id`：接替本轮的新轮次。没有关联时为 `null`。
- `last_sequence`：快照对应的最新事件序号；`earliest_sequence`：缓存最早可用事件序号。
- `messages`：按生成顺序排列，每条含 `message_id/message_kind/text/status/references`，状态为 `streaming/completed/interrupted`。
- `progress`、`error`：各自最近一次事件负载，没有则为 `null`，负载保留 `event_type` 标签。校验结论作为普通消息保存在 `messages`，不另设专属字段。

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

`200 application/json`，模型 `CommunicationSnapshot`。未激活就取消的完整示例：

```json
{
  "conversation_id": "c1",
  "turn_id": "t1",
  "status": "cancelled",
  "activated_at": null,
  "finished_at": "2026-09-08T09:01:00Z",
  "processing_duration_ms": 0,
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
| 409 | 已处于 completed、superseded、rejected、failed 或 expired 终态。 |
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
| `pending_activation` | 进入 `cancelled`，释放输入；此后订阅只回放取消终态，绝不激活。 |
| `processing` | 进入 `cancelled`，释放输入，活跃消息标为 `interrupted`；已连接 SSE 收到终态后关闭。 |
| `cancelled` | 幂等返回相同快照，不追加事件、不刷新保留时间。 |
| 其他终态 | 返回 `409`，不覆盖完成、替代、拒绝、失败或过期记录。 |
| 不存在或跨用户、跨会话 | 返回 `404`，不修改任何状态。 |

认证失败返回 `401`。激活超时已经生效时，取消按 `expired` 返回 `409`；保留期结束后返回 `404`。取消与激活、替代、完成共用锁：激活先发生则取消处理中的轮次，取消先发生则后续不激活；与其他终态竞争时先提交者生效。终态后禁止旧事件继续发布。

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
- [样式联调说明](../capability/application/communication-ui-demo.md)：模拟输出与场景配置。
