# 合同 API

> **用途：** 本文是前端接入 Core 表单定义、合同上传、合同文档识别、查重暂停与继续、结构识别、建议名称、处理进度、Core/Clause 提取结果、失败阶段断点重试和正式入库的 HTTP/SSE 参考。全局接口约定见[API 参考](readme.md)，内部状态机见[合同提取应用运行时](../architecture/system/contract-extraction-runtime.md)。

---

## 资源约定

- 本文所有接口都必须携带 `Authorization: Bearer <login_code>`；免登码通过[审核用户登录接口](auth.md)取得。
- 查询接口对三个等级开放；上传、继续、重试、正式入库和取消未入库任务仅限 1、2 级，3 级访问返回 `403`。等级不会绕过任务所有者隔离。删除正式合同仅限 1 级，取消任务不等于删除正式合同。权限定义见[用户权限等级](../architecture/data/reviewer-user-definition.md#权限等级)。
- `run_id` 是一次上传任务的唯一标识，不等同于合同 `document_id`。
- 创建任务时，服务端会把免登码解析出的审核人名称写为任务所有者；客户端不提交、也不能覆盖该名称。
- 运行列表以及所有携带 `run_id` 的查询、SSE 和状态变更接口只允许任务所有者访问。其他审核用户访问时与任务不存在一样返回 `404`，不会泄露 `run_id` 是否真实存在。
- `document_id` 是任务实际保存并计划入库的处理版 PDF 字节 SHA-256。
- SSE 及时传递状态事件、紧凑的合同文档判断、查重审核结果、分类结果和建议文件名；单任务快照持续保存分类与建议名称，Core 与 Clause 提取值也只能通过快照接口获取。
- 原始 PDF 只在创建请求期间保留；任务保存按视觉预算栅格化的处理版 PDF、同源页面缓存、处理状态和草稿。

---

## 获取所有已入库合同元数据

```http
GET /contract/api/contract/documents
```

无需路径参数、查询参数或请求体。返回 SQLite 中所有 `ready` 合同，不分页、不按审核人过滤；正在入库的 `ingesting` 记录不可见。所有已登录审核人均可读取该共享正式合同目录，这与按所有者隔离的内存运行列表不同。

成功状态码为 `200 OK`，响应为数组，无已入库合同时返回 `[]`。沿用存储目录顺序：`ingested_at` 降序，同值时 `document_id` 升序。

```json
[
  {
    "document_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "file_name": "设备采购合同",
    "category": "sale / construction",
    "contract_time": "2026-09-07",
    "file_uri": "/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.pdf",
    "reviewer": "审核人甲",
    "ingested_at": "2026-09-07T06:00:00Z"
  }
]
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `document_id` | string | 处理版 PDF 的 64 位小写 SHA-256 文档标识。 |
| `file_name` | string | 用户最终确认的展示名称。 |
| `category` | string | 类别 `code` 以 ` / ` 连接；不是数组或类别 ID。未映射时保留类型说明。 |
| `contract_time` | string 或 null | 签订日期，格式为 `YYYY-MM-DD`；缺失时返回 `null`。 |
| `file_uri` | string | 处理版 PDF 的稳定根相对读取地址。 |
| `reviewer` | string | 确认最终结果并执行入库的审核人名称。 |
| `ingested_at` | string | 带时区的 ISO 8601 入库时间。 |

响应每项仅包含以上七个必有字段，不返回入库状态、尝试标识、分类理由或完整 Core/Clause。接口复用 `SQLiteContractMetadataStore.list_ready()`，同步查询在线程池中执行，不访问 Elasticsearch 或读取 PDF。当前不提供筛选与分页。

```bash
curl --header 'Authorization: Bearer <login_code>' \
  'http://127.0.0.1:20000/contract/api/contract/documents'
```

缺少、无效或过期的 Bearer 免登码返回 `401`。

---

## 删除正式合同

```http
DELETE /contract/api/contract/documents/{document_id}
```

仅限 1 级用户，可删除任意审核人已入库的正式合同。路径参数 `document_id` 必须是 64 位小写十六进制 SHA-256；不接受文件名或文件路径，无查询参数及请求体。

按顺序删除正式 Elasticsearch 索引中的同 ID 文档、`data/contract/<document_id>.pdf`、SQLite 合同摘要；类别关联由外键级联清除，全局类别字典保留。成功返回 `204 No Content`，无响应体。此操作不自动备份，也不删除历史备份、实验产物或其他内存提取任务。

| 状态码 | 含义 |
| --- | --- |
| `401` | 未登录或免登码无效。 |
| `403` | 当前用户不是 1 级。 |
| `404` | SQLite 中不存在该合同，包括删除成功后重复请求。 |
| `409` | 合同尚未完成入库，当前不能删除。 |
| `422` | 文档 ID 格式错误。 |
| `502` | 存储操作失败，可能部分完成，应使用同一 ID 重试。 |

ES 或 PDF 已不存在不阻断剩余清理。SQLite 始终最后删除，中途失败时保留摘要与类别关联作为重试入口；此时列表可能仍显示该合同，但 PDF 或 ES 已不可用。没有跨存储原子回滚或自动删除恢复任务，重启后仍需重新调用本接口完成清理。相同 ID 的删除与入库在当前单进程内串行。

```bash
curl --request DELETE --header 'Authorization: Bearer <login_code>' \
  'http://127.0.0.1:20000/contract/api/contract/documents/<document_id>'
```

---

## 获取合同类别列表

```http
GET /contract/api/contract/categories
```

无需路径参数、查询参数或请求体。读取 SQLite 的 `contract_category_metadata`，返回全部类别（包括尚未关联任何合同的类别），按 `category_id` 升序排列，不分页。该表在应用启动时从权威类别目录同步；接口不查询 Elasticsearch，也不返回模型分类理由。

成功状态码为 `200 OK`，响应为数组；类别表为空时返回 `[]`。以下为单项结构示例，具体值以接口返回为准：

```json
[
  {"category_id": 1, "code": "example_category", "name": "示例合同类别"}
]
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `category_id` | integer | 当前 SQLite 数据库中的正整数类别主键，可与合同类别关联表对应；不同数据库之间不保证一致。 |
| `code` | string | 权威类别目录的稳定代码，可与提取结果的类别 `code` 对应。 |
| `name` | string | 类别标准中文名称，供前端展示。 |

所有字段均必有且非空。缺少、无效或过期的 Bearer 免登码返回 `401`，沿用统一认证约定。

```bash
curl --header 'Authorization: Bearer <login_code>' \
  'http://127.0.0.1:20000/contract/api/contract/categories'
```

本接口仅提供类别选项，不查询合同列表，也不新增或修改类别。

---

## 获取 Core 审核表单定义

```http
GET /contract/api/contract/core-definitions
```

该接口直接投影应用启动时已加载并校验的 Core 定义快照，不重复读取 YAML。响应顺序与定义目录的稳定加载顺序一致，并且在当前进程生命周期内不变。

### 成功响应

状态码：`200 OK`

```json
[
  {
    "code": "contract_subjects",
    "name": "合同标的",
    "cardinality": "multiple",
    "properties": [
      {
        "code": "name",
        "name": "标的名称",
        "type": "string",
        "required": true
      },
      {
        "code": "quantity",
        "name": "数量",
        "type": "number",
        "required": false
      }
    ]
  }
]
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `code` | string | 与提取结果及 Elasticsearch Core 使用的稳定英文键一致。 |
| `name` | string | 审核界面展示的中文名称。 |
| `cardinality` | enum | `single` 表示单项；`multiple` 表示前端可增删的多项列表。 |
| `properties` | array | 每一项允许填写的扁平属性，至少包含一项。 |
| `properties[].type` | enum | `string`、`integer`、`number` 或 `boolean`，用于选择输入控件并校验提交值。 |
| `properties[].required` | boolean | 当前 Core 项非空时，该属性是否必须填写。 |

前端应按字段 `code` 将该目录与快照中的 `draft.core` 绑定。`single` 且只有一个属性的字段在 Core 结果中使用标量；`single` 且有多个属性时使用对象；`multiple` 始终使用对象数组。即使提取值为 `null`，前端也能通过本接口确定输入结构。

接口只公开生成审核表单所需的结构信息，不返回模型提示语义、别名、排除规则或 Elasticsearch 分词配置。

---

## 列出尚未入库的运行

```http
GET /contract/api/contract/extraction-runs
```

该路径的 `GET` 用于恢复运行，`POST` 仍用于上传 PDF 并创建新运行。列表只返回当前登录审核用户创建，并且正在处理、等待查重确认或已经形成 Core/Clause 结果但尚未入库的任务；其他用户的任务不会出现在结果中。

### 成功响应

状态码：`200 OK`

```json
[
  {
    "run_id": "f98a2b1d-...",
    "document": {
      "file_id": "f98a2b1d-...",
      "file_name": "设备采购合同.pdf",
      "processed_file_size_bytes": 824301,
      "page_count": 12,
      "cover_width_pixels": 1240,
      "cover_height_pixels": 1754
    },
    "suggested_file_name": "工业机械臂采购合同",
    "status": "blocked",
    "created_at": "2026-09-02T10:00:00Z",
    "updated_at": "2026-09-02T10:05:00Z",
    "expires_at": "2026-09-02T11:05:00Z"
  }
]
```

返回项按 `updated_at` 倒序排列，`status` 只可能是：

- `processing`：后台拓扑仍在自动推进，至少还有公共流程或业务分支正在执行；
- `blocked`：后台不会继续自动推进，列表可展示为“待处理”；包括等待确认、可重试失败、待复核入库，以及仅可查看或取消清理的非合同、重复拒绝终态。该状态不代表允许继续提取。

合同结构识别、合同分类等公共前置阶段失败时，任务会继续出现在列表中并标记为 `blocked`，避免因为没有形成草稿而从恢复入口消失。某个业务分支失败、但其他分支仍在执行时暂时保持 `processing`；所有自动执行停止后转为 `blocked`。非合同（`not_a_contract`）和重复拒绝（`duplicate_rejected`）在保留期内也返回，列表均标记为 `blocked`。已取消、已过期回收及已入库释放的任务不返回。读取列表不会刷新任务 TTL；列表为空时直接返回 `[]`。

列表状态是面向恢复入口的粗粒度投影，不替代单任务快照中的 `run.status` 和八个阶段状态。`suggested_file_name` 在命名成功前为 `null`，成功后返回不带扩展名的名称摘要；完整理由和证据仍需查询单任务快照。前端选择一项后，应以其 `run_id` 查询快照来判断具体阻塞位置、可重试阶段和已有结果。非合同与重复拒绝只展示终态说明及 PDF，隐藏继续、重试和入库操作，允许主动取消清理；其他任务按快照能力显示操作并订阅 SSE。

`document` 是任务实际持有的处理版 PDF 元数据，在创建响应、列表项和单任务快照中使用同一结构：

| 字段 | 说明 |
| --- | --- |
| `file_id` | 内存处理版 PDF 的 UUID，复用本轮 `run_id`，用于资源接口读取。 |
| `file_name` | 创建任务时保存的用户文件名。 |
| `processed_file_size_bytes` | 压缩并重新封装后的 PDF 字节数，不是原始上传大小。 |
| `page_count` | 处理版 PDF 的物理页数。 |
| `cover_width_pixels` | 处理后第 1 页图像的像素宽度。 |
| `cover_height_pixels` | 处理后第 1 页图像的像素高度。 |

这个运行期 `document` 对象只服务恢复和前端展示，不会被整体复制到 Elasticsearch。正式入库会从中使用处理版页数，并以用户最终提交的 `file_name` 替代创建任务时的原始文件名。

恢复 PDF 预览时，使用 `file_id` 请求 `GET /contract/api/resource/extraction-pdf/{file_id}`，携带同一用户的 Bearer 免登码。读取原有内存处理版，不重新渲染；完整协议与生命周期见[内存处理版 PDF 读取](resource.md#读取提取任务的内存处理版-pdf)。

> **入库后释放：** 正式入库成功后，入库服务会从内存注册表删除对应 `run_id`；该任务随后不再出现在本列表中。

---

## 上传 PDF 并创建任务

```http
POST /contract/api/contract/extraction-runs?file_name={file_name}
Content-Type: application/pdf
```

### 请求

| 位置 | 名称 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| Query | `file_name` | string | 是 | 前端展示使用的原始文件名，长度为 1～255。 |
| Body | PDF 字节 | binary | 是 | 非空的原始 PDF 请求体。 |

接口不接收服务器文件路径，也不使用 `multipart/form-data`。浏览器可以直接把 `File` 对象作为 `fetch` 的 `body`。请求不提供用户名字段；服务端使用当前 Bearer 免登码对应的审核人名称标记任务。

创建接口会等待内部 PDF 技术服务完成格式、加密状态、页数、视觉预算和页面渲染检查。该步骤只是创建任务的输入校验与标准化，不作为“合同预处理”用户阶段或 SSE 状态暴露。无法读取、损坏或加密的 PDF 不会创建任务，返回 `422`。文件大小、恶意内容和更细业务规则仍未实施独立上传策略。

### 示例

```bash
curl --request POST \
  --header 'Authorization: Bearer <login_code>' \
  --header 'Content-Type: application/pdf' \
  --data-binary '@contract.pdf' \
  'http://127.0.0.1:10000/contract/api/contract/extraction-runs?file_name=contract.pdf'
```

### 成功响应

状态码：`202 Accepted`

响应是创建时刻的 `ContractExtractionSnapshot`。此时 `contract_document_detection` 已进入 `running`，后台判断上传内容是否属于合同文档；查重及全部提取阶段均为 `pending`。只有可靠判定为合同时才会自动开始查重。前端应立即订阅 SSE 或查询快照。

```json
{
  "run": {
    "run_id": "f98a2b1d-...",
    "status": "processing",
    "created_at": "2026-08-29T10:00:00Z",
    "updated_at": "2026-08-29T10:00:00Z",
    "expires_at": "2026-08-29T11:00:00Z",
    "document": {
      "file_id": "f98a2b1d-...",
      "file_name": "contract.pdf",
      "processed_file_size_bytes": 824301,
      "page_count": 12,
      "cover_width_pixels": 1240,
      "cover_height_pixels": 1754
    },
    "stages": {
      "contract_document_detection": {
        "code": "contract_document_detection",
        "name": "确认合同文档",
        "status": "running",
        "message": "正在确认上传内容是否属于合同文档。",
        "attempt": 1,
        "retryable": false,
        "progress": null,
        "result_status": null,
        "result_revision": null,
        "started_at": "2026-08-29T10:00:00Z",
        "updated_at": "2026-08-29T10:00:00Z"
      }
    },
    "available_sections": [],
    "document_detection": null,
    "deduplication": null,
    "classification": null,
    "suggested_file_name": null
  },
  "draft": null
}
```

实际 `stages` 始终包含八个阶段：`contract_document_detection`、`pdf_deduplication`、`contract_structure_recognition`、`contract_classification`、`file_name_generation`、`core_extraction`、`clause_extraction` 和 `retrieval_preparation`。示例只展开其中一个以说明结构。

### 无法处理的 PDF

状态码：`422 Unprocessable Content`

响应 `detail` 会说明文件损坏、不是 PDF、已加密或不包含页面等可执行原因；失败请求不会分配 `run_id`。

---

## 获取当前状态与提取结果

```http
GET /contract/api/contract/extraction-runs/{run_id}
```

### 成功响应

状态码：`200 OK`

响应在同一聚合锁下生成，`run` 与 `draft` 属于同一时刻。`run.classification` 在分类成功前为 `null`，成功后持续返回精简分类结果；`run.suggested_file_name` 在命名成功前为 `null`，成功后持续返回名称、理由和页面证据。`draft` 在 Core 或 Clause 首次形成可用结果前为 `null`；之后只包含当前可用的 `core` 和 `clauses`，不公开检索问题、向量或内部工具状态。

```yaml
run:
  run_id: f98a2b1d-...
  status: partial_ready
  created_at: "2026-08-29T10:00:00Z"
  updated_at: "2026-08-29T10:03:00Z"
  expires_at: "2026-08-29T11:03:00Z"
  document:
    file_id: f98a2b1d-...         # 与本轮 run_id 相同，资源读取使用此 UUID
    file_name: 设备采购合同.pdf
    processed_file_size_bytes: 824301
    page_count: 12
    cover_width_pixels: 1240
    cover_height_pixels: 1754
  stages: {}                     # 八个 StageSnapshot，以 code 为键
  available_sections: [core]
  document_detection: {}         # 合同文档判断完成后保留；完成前为 null
  deduplication: {}              # 查重完成后保留；完成前为 null
  classification:               # 分类完成后保留；完成前为 null
    status: classified
    categories:
      - code: sale
        name: 买卖合同
        scenario: 甲方向乙方采购设备
    unmapped_type_description: null
  suggested_file_name:          # 建议命名完成后保留；完成前为 null
    file_name: 工业机械臂采购合同
    reasoning: 原标题仅表示通用文种，页面明确标的是工业机械臂。
    evidence:
      - page_number: 1
        content: 产品名称：六轴工业机械臂
draft:
  core:
    contract_name: 设备采购合同
    contract_number: null
    contract_total_amount: 1200000
  clauses: null
```

`core` 和 `clauses` 独立就绪，因此其中一个可以暂时为 `null`；`run.available_sections` 也只会出现 `core`、`clause`。`run.classification` 和 `run.suggested_file_name` 分别与对应 SSE 完成事件使用同一个公共投影，但不进入 Core/Clause `draft`。前端可以用建议名称初始化可编辑文件名；正式入库请求仍以用户提交值为准，服务端不会强制其等于建议名称。

### 运行状态

| 值 | 含义 |
| --- | --- |
| `processing` | 尚无可用 Core/Clause 结果，任务仍在处理。 |
| `not_a_contract` | 上传内容已被可靠判定为非合同，后续处理已停止。 |
| `duplicate_rejected` | 哈希一致或模型判定为重复，保留候选展示，但禁止继续、重试及入库。 |
| `awaiting_deduplication_review` | 查重结果已返回，结构识别尚未启动，正在等待前端处理候选并调用继续接口。 |
| `partial_ready` | 至少一个分区可查看，其他分支仍在处理或失败。 |
| `ready` | 建议名称成功，且三个业务分支均已形成当前结果。 |
| `failed` | 串行公共处理或建议名称失败，或三个业务分支均未形成结果。 |
| `cancelled` | 只会在现有连接的取消 SSE 事件中观察到；随后查询返回 `404`。 |
| `expired` | 只会在到期 SSE 事件中观察到；随后查询返回 `404`。 |
| `ingested` | 只会在现有连接的正式入库终态事件中观察到；随后查询返回 `404`。 |

### 合同文档识别结果

识别完成后，快照中的 `run.document_detection` 保存紧凑的二分类结果：

```json
{
  "is_contract": false,
  "evidence": [
    {
      "page_number": 1,
      "observation": "页面为单方开具的付款收据，未呈现双方约定义务。"
    }
  ],
  "reasoning_summary": "文档仅记录付款事实，没有相对方协议性权利义务结构。"
}
```

`is_contract=true` 时服务自动开始查重。`is_contract=false` 时运行进入 `not_a_contract`，查重、结构识别、分类、建议名称及三个提取分支均不启动。页面不可读、模型请求失败或没有形成有效工具决定属于 `contract_document_detection` 阶段技术失败，不会返回伪造的 `false`。

### 查重暂停结果

`pdf_deduplication` 完成后，快照中的 `run.deduplication` 与 SSE 事件返回同一个紧凑结果。有重复候选时发送终态事件 `run.duplicate_rejected`；否则发送暂停事件 `run.deduplication_review_required`。以下为重复合同结果：

```json
{
  "status": "duplicate",
  "candidates": [
    {
      "rank": 1,
      "cosine_similarity": 0.923886,
      "relation": "duplicate",
      "document_id": "e7591f0d...",
      "file_name": "设备采购合同（友好名称）.pdf",
      "file_uri": "/e7591f0d....pdf",
      "reviewer": "张三",
      "page_count": 21,
      "reasoning_summary": "合同身份和核心交易条款一致，属于同一合同的完整版本。"
    }
  ],
  "review_expires_at": null,
  "can_continue": false,
  "continued_at": null
}
```

- `candidates` 来自 ES Top-3 召回，但只保留精确哈希命中或模型可靠判定为重复、相似的合同，因此数量为 0～3，原始 `rank` 可以不连续。
- `cosine_similarity` 是 `[-1, 1]` 的原始 cosine，不是 Elasticsearch 变换后的 `_score`。
- `relation` 只可能是 `duplicate` 或 `similar`。
- `document_id` 与上传处理版 PDF 完全一致时，服务依据 SHA-256 文件身份直接形成 `duplicate`，不加载候选文件或调用 MLLM；其他候选仍执行视觉判断。
- `file_name`、`file_uri` 和 `reviewer` 均直接来自该候选的 Elasticsearch 文档；`reviewer` 表示确认该合同最终结果并执行入库的审核人。服务不使用哈希文件名覆盖友好展示名称，也不生成运行级下载地址。
- `different` 和 `failed` 判断仍保留在查重内部结果及私有审计中，但不会返回前端。
- 每个返回候选都提供 `reasoning_summary`；精确哈希命中只说明文件身份一致，MLLM 判断也不公开完整工具轨迹。
- 前端需要预览 PDF 时，将 `file_uri` 作为查询参数传给[资源文件 API](resource.md)，即 `GET /contract/api/resource/contract?file_uri=...`。SSE 不内联 PDF 二进制或 Base64。
- `review_expires_at` 在暂停点形成后固定，GET、SSE 和心跳不会刷新。成功继续后 `continued_at` 记录实际消费暂停点的时间。
- 重复合同不进入暂停点：`can_continue=false`、`review_expires_at=null`、`continued_at=null`，运行状态为 `duplicate_rejected`。候选 PDF 列表、负责人和理由继续展示，快照及事件保留至普通运行 TTL 到期；保留期内仍进入可恢复运行列表，列表状态为 `blocked`。SSE 发出终态事件后关闭，重连可回放。前端须接收新事件并隐藏继续、重试和入库操作，而不是隐藏候选列表。
- 非重复（包括仅 `similar`）仍按原流程暂停，此时 `can_continue=true`；成功继续后变为 `false`。重复结果不可人工放行，即使之后删除候选，原任务也不能继续，需要重新上传发起查重。继续、重试和入库入口均在后端校验重复结果并返回 `409`。

### 阶段对象

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `code` | enum | 稳定机器标识。 |
| `name` | string | 非技术用户可读名称。 |
| `status` | enum | `pending`、`running`、`succeeded`、`failed` 或 `retrying`。 |
| `message` | string | 可直接展示给用户的简洁说明。 |
| `attempt` | integer | 已开始的尝试次数；未开始为 `0`。 |
| `retryable` | boolean | 当前状态是否允许提交独立重试。 |
| `progress` | object/null | 仅真实总量可知时包含 `completed` 和 `total`。 |
| `result_status` | enum/null | 已提交分区为 `completed` 或 `partial`。 |
| `result_revision` | integer/null | 当前成功分区的独立修订号。 |
| `started_at` | datetime/null | 当前尝试的开始时间；阶段未开始时为 `null`，重试时替换为新尝试的开始时间。 |
| `updated_at` | datetime | 当前阶段最后更新时间。 |

SSE 的 `stage` 和 GET 快照中的阶段对象使用相同时间口径。阶段处于 `running` 或 `retrying` 时，前端可用“当前时间减 `started_at`”动态计时；阶段进入 `succeeded` 或 `failed` 后，用“`updated_at` 减 `started_at`”固定本次耗时。`pending` 阶段不启动计时。重试会从本次尝试重新计时，而不是把此前失败尝试累计到当前计时中。

### Core 分区

```yaml
core:
  contract_name: 设备采购合同
  contract_number: null
  contract_subjects:
    - type: 设备
      name: 工业机械臂
      quantity: 2
      unit: 台
  contract_total_amount: 1200000
```

Core 使用字段定义中的稳定英文 `code`，值的标量、对象或数组形状与 Elasticsearch Core mapping 一致。只要 Core 分支形成 `completed` 或 `partial` 结果，全部配置字段都会出现；没有可靠值的字段使用 `null`，失败字段存在已校验部分对象时保留该部分值。`null` 只用于用户审核占位，正式入库投影会将其过滤，不写入 Elasticsearch。

### Clause 分区

```yaml
clauses:
  - clause_id: clause-0001
    order: 1
    identifier: "第一条"
    title: 合同标的
    path: ["第一条 合同标的"]
    parent_clause_id: null
    level: 1
    start_page: 1
    end_page: 1
    content: "第一条 合同标的……"
```

`clauses` 与 Elasticsearch 同名字段使用相同的条款元素结构，只保留成功提取的条款。失败候选不混入数组；前端通过 `run.stages.clause_extraction.result_status` 判断结果是否为 `partial`。标题或父条款不存在时对应字段可以为 `null`，正式入库时再省略空可选字段。

### 错误响应

| 状态码 | 条件 |
| --- | --- |
| `404` | `run_id` 不存在、已经从内存释放，或不属于当前审核用户。 |

---

## 取消任务

```http
DELETE /contract/api/contract/extraction-runs/{run_id}
```

请求没有 Body。当前审核用户可以取消自己仍驻留内存的任意任务，包括正在自动执行、等待查重确认、失败阻塞或已经形成草稿的任务。

成功时返回 `204 No Content`。服务会取消该 `run_id` 的后台处理协程和查重到期计时器，向已经建立的 SSE 连接发送一次 `run.cancelled` 后结束连接，并从注册表删除处理版 PDF、页面、模型中间结果、草稿和事件缓冲。取消完成后，列表不再返回该任务，快照、继续、重试、再次取消和重新订阅 SSE 均按任务不存在处理。

取消是终止操作，不支持恢复；如需重新处理，必须重新上传 PDF。接口继续采用任务所有权隔离，其他审核用户请求同样返回 `404`。

| 状态码 | 条件 |
| --- | --- |
| `204` | 任务已取消并从当前进程内存释放。 |
| `404` | 任务不存在、已经结束，或不属于当前审核用户。 |

---

## 确认查重并继续

```http
POST /contract/api/contract/extraction-runs/{run_id}/continue
```

请求没有 Body。只有 `awaiting_deduplication_review` 状态接受该操作；成功后返回 `202 Accepted` 和当前快照，运行恢复普通 TTL，`contract_structure_recognition` 已进入 `running`。结构识别成功后服务自动开始分类。暂停点只能消费一次，重复提交不具备幂等成功语义。

非重复任务可以在调用继续接口前处理相似候选；这些外部操作不写回本次运行，也不自动触发继续。重复任务永久禁止继续，即使已删除原候选也必须重新上传。非重复任务等待超过 `review_expires_at` 后整个运行从内存释放，必须重新上传。

| 状态码 | 条件 |
| --- | --- |
| `202` | 暂停点已消费，后台开始结构识别，随后继续分类和提取。 |
| `404` | 运行不存在、不属于当前审核用户，或 10 分钟暂停期限已经结束。 |
| `409` | 运行尚未到达暂停点、已经继续或当前状态不允许继续。 |

---

## 订阅处理事件

```http
GET /contract/api/contract/extraction-runs/{run_id}/events
Accept: text/event-stream
Last-Event-ID: 11
```

`Last-Event-ID` 可省略。提供时必须是非负整数，服务只回放缓冲区中 `sequence` 更大的事件，然后继续发送实时事件。

> **SSE 客户端：** 浏览器原生 `EventSource` 不能设置 `Authorization` 请求头。订阅受保护的事件流时，应使用支持自定义请求头的 fetch 流式客户端。免登码只在建立 SSE 请求时校验和刷新一次，不会因后续心跳或业务事件持续续期。

### 业务事件帧

```text
id: 12
event: stage.completed
data: {"sequence":12,"run_id":"...","event_type":"stage.completed","overall_status":"processing","message":"合同类型已识别。","stage":{...},"draft_revision":null,"available_sections":[],"classification":{"status":"classified","categories":[{"code":"sale","name":"买卖合同","scenario":"甲方向乙方采购设备"}],"unmapped_type_description":null},"occurred_at":"2026-08-29T10:02:00Z"}

```

`event` 与 JSON 中的 `event_type` 保持一致。业务事件包括：

| 事件 | 前端动作建议 |
| --- | --- |
| `run.started` | 初始化任务时间线。 |
| `stage.started` | 展示当前阶段正在处理。 |
| `stage.progress` | 更新阶段消息或真实离散进度。 |
| `stage.completed` | 标记阶段成功；分类和建议名称阶段分别同时展示对应公共结果。 |
| `stage.failed` | 展示失败消息，并依据 `retryable` 决定是否显示重试。 |
| `stage.retrying` | 标记已接受重试。 |
| `run.document_rejected` | 上传内容不是合同；展示 `document_detection` 的证据与理由并停止等待后续阶段。 |
| `run.duplicate_rejected` | 已发现重复合同；继续展示 `deduplication.candidates`，停止后续处理并关闭 SSE。 |
| `run.deduplication_review_required` | 渲染 `deduplication` 候选，暂停其他阶段并提示用户在截止时间前处理和继续。 |
| `run.continued` | 标记暂停点已经消费，继续展示结构识别、分类和提取进度。 |
| `draft.updated` | 调用快照接口获取最新 Core 与 Clause 提取值。 |
| `run.review_ready` | 首份草稿已可查看。 |
| `run.cancelled` | 当前任务已由用户取消；结束订阅并从界面移除该任务。 |
| `run.expired` | 结束订阅并提示重新上传。 |
| `run.ingested` | 合同已正式入库且 `run_id` 已释放；结束订阅并从界面移除该任务。 |

非合同终态直接携带合同文档识别结果：

```text
event: run.document_rejected
data: {"event_type":"run.document_rejected","overall_status":"not_a_contract","document_detection":{"is_contract":false,"evidence":[...],"reasoning_summary":"..."}}
```

合同识别成功且 `is_contract=true` 时，前端根据随后的 `pdf_deduplication` 阶段事件继续展示进度；断线时可以从 GET 快照的 `run.document_detection` 恢复判断结果。

查重暂停事件直接携带查重审核结果：

```text
event: run.deduplication_review_required
data: {"event_type":"run.deduplication_review_required","overall_status":"awaiting_deduplication_review","deduplication":{"status":"unique","candidates":[...],"can_continue":true,"review_expires_at":"...","continued_at":null}}
```

`stage.completed` 只表示 `pdf_deduplication` 阶段成功；前端应以随后的 `run.deduplication_review_required` 渲染候选并停止等待分类进度。断线漏掉该事件时，通过 GET 快照读取 `run.status` 和 `run.deduplication` 恢复。

合同分类成功时，对应的 `stage.completed` 事件携带一次性 `classification`：

```json
{
  "event_type": "stage.completed",
  "stage": {
    "code": "contract_classification",
    "status": "succeeded"
  },
  "classification": {
    "status": "classified",
    "categories": [
      {
        "code": "sale",
        "name": "买卖合同",
        "scenario": "甲方向乙方采购设备"
      }
    ],
    "unmapped_type_description": null
  }
}
```

`classification.status` 可以是 `classified`、`unmapped` 或 `partial`。命中类别时 `categories` 可以包含一项或多项；未命中权威类别时数组为空，并通过 `unmapped_type_description` 提供简短类型说明。分类证据、失败类别和工具轨迹不公开。

相同的精简负载也会在分类成功后持续出现在单任务 GET 快照的 `run.classification` 中，但不进入最终可编辑的 `draft`。因此 SSE 用于及时展示，断线恢复或事件缓冲被淘汰后仍可通过快照同步分类结果。

建议名称成功时，`file_name_generation` 的 `stage.completed` 事件携带 `suggested_file_name`：

```json
{
  "event_type": "stage.completed",
  "stage": {
    "code": "file_name_generation",
    "status": "succeeded"
  },
  "suggested_file_name": {
    "file_name": "工业机械臂采购合同",
    "reasoning": "原标题只表达采购文种，页面明确记载了核心设备。",
    "evidence": [
      {
        "page_number": 1,
        "content": "产品名称：六轴工业机械臂"
      }
    ]
  }
}
```

`file_name` 只包含名称主体，不带扩展名；`reasoning` 是可供用户理解命名依据的简洁理由；`evidence` 使用从 1 开始的合同物理页码和短原文。相同对象持续保存在 GET 快照的 `run.suggested_file_name` 中；运行历史列表只返回名称字符串摘要。SSE 断线或事件超出缓冲后，前端必须以快照为准恢复该值。用户在最终入库前可以任意修改名称，入库接口不会比较建议值与最终值。

### 并行内容进度

`contract_classification`、`core_extraction`、`clause_extraction` 和 `retrieval_preparation` 会通过连续的 `stage.progress` 表达真实离散进度：

| 顺序 | `message` 语义 | `stage.progress` |
| --- | --- | --- |
| 1 | 正在统计待处理内容数量 | `null` |
| 2 | 数量统计完成，共需处理 `m` 项 | `{"completed":0,"total":m}` |
| 3 | 正在处理，已完成 `k / m` | `{"completed":k,"total":m}` |

分类和 Core 的总数来自当前不可变目录。条款总数在候选发现完成后确定；检索问题总数在问题规划完成后确定。因此后两类阶段可能在“正在统计数量”停留较长时间，前端不能自行估算百分比。

单项产生受控失败结果也表示该并行任务已经形成终态，会推进 `completed`；阶段最终可能以 `result_status: partial` 完成。`retrieval_preparation` 达到 `m / m` 后仍需完成问题向量化与合同向量融合，前端应以 `stage.completed` 判断整个阶段结束。

```text
event: stage.progress
data: {"event_type":"stage.progress","message":"数量统计完成，共需提取 12 个核心字段。","stage":{"code":"core_extraction","status":"running","progress":{"completed":0,"total":12}}}

event: stage.progress
data: {"event_type":"stage.progress","message":"正在提取核心字段，已完成 5 / 12。","stage":{"code":"core_extraction","status":"running","progress":{"completed":5,"total":12}}}
```

### 心跳帧

```text
event: heartbeat
data: {"run_id":"...","occurred_at":"2026-08-29T10:02:15Z"}

```

心跳没有 `id`，不进入业务事件序列，也不触发草稿刷新。

### 响应头

服务返回：

```text
Content-Type: text/event-stream
Cache-Control: no-cache, no-transform
X-Accel-Buffering: no
```

若断线时间超过事件缓冲范围，前端应先请求一次全量快照恢复权威状态，再携带最新已处理事件序号重新订阅。

### 错误响应

| 状态码 | 条件 |
| --- | --- |
| `400` | `Last-Event-ID` 不是非负整数。 |
| `404` | `run_id` 不存在、已经过期，或不属于当前审核用户。 |

---

## 重试失败阶段

```http
POST /contract/api/contract/extraction-runs/{run_id}/stages/{stage_code}/retry
```

`stage_code` 接受八个用户业务阶段：

- `contract_document_detection`；
- `pdf_deduplication`；
- `contract_structure_recognition`；
- `contract_classification`；
- `file_name_generation`；
- `core_extraction`；
- `clause_extraction`；
- `retrieval_preparation`。

只有状态为 `failed` 且尚未达到尝试上限的阶段可以重试。成功接受后返回 `202 Accepted` 和当前快照，目标阶段已经是 `retrying`；服务复用该阶段之前已经成功的权威结果，不回退或重跑更早阶段。重试阶段成功后自动恢复正常后续流程：结构识别重试成功后继续分类，分类重试成功后继续建议命名，建议命名重试成功后启动三个提取分支。

> **一次成功即终止重跑：** 阶段只要有一次进入 `succeeded`，即使业务结果完整程度是 `partial`，也不再允许重跑。重试期间，其他已经成功的草稿分区继续可读；目标阶段只有成功后才会提交自己的结果。

### 错误响应

| 状态码 | 条件 |
| --- | --- |
| `404` | `run_id` 不存在、已经过期，或不属于当前审核用户。 |
| `409` | 阶段未失败、成功前置结果缺失，或已经达到最大尝试次数。 |
| `422` | `stage_code` 不是八个用户业务阶段之一。 |

---

## 正式入库合同

```http
POST /contract/api/contract/extraction-runs/{run_id}/ingestion
Content-Type: application/json
```

八个用户业务阶段全部为 `succeeded` 后，审核用户提交最终展示文件名以及完整 Core、Clause。请求体不接受建议名称、`document_id`、分类、检索问题、向量、PDF 地址、审核人或入库时间，这些信息均由服务端根据当前运行和登录用户补齐。最终 `file_name` 可以沿用、修改或完全替换自动建议。生成问题原文由服务端保存至 ES `retrieval_questions` 数组，供后续更换模型重算向量，无需前端新增字段。

`core.signing_date` 是入库必填项，必须提供完整合法的签订日期。缺失、`null`、空白或非法日期返回 `422`，不会写入任何正式存储；日期统一规范为 `YYYY-MM-DD`。提取结果仍可能为 `null`，前端需提示审核人依据合同补充日期后再入库，不得自动用当前日期或其他业务日期替代。历史合同查询仍兼容空日期。

```json
{
  "file_name": "设备采购合同",
  "core": {
    "contract_name": "设备采购合同",
    "contract_number": null,
    "contract_subjects": [
      {
        "type": "设备",
        "name": "工业机械臂",
        "quantity": 2,
        "unit": "台"
      }
    ],
    "contract_total_amount": 1200000,
    "currency": "CNY",
    "related_parties": null,
    "signed": null,
    "signing_date": "2026-09-07",
    "tax_included": true,
    "tax_rate": 13
  },
  "clauses": [
    {
      "clause_id": "clause-0001",
      "order": 1,
      "identifier": "第一条",
      "title": "合同标的",
      "path": ["第一条 合同标的"],
      "parent_clause_id": null,
      "level": 1,
      "start_page": 1,
      "end_page": 1,
      "content": "第一条 合同标的……"
    }
  ]
}
```

`core` 必须包含 Core 定义接口返回的全部顶层 `code`，未知或缺失字段都会被拒绝；没有最终值时提交 `null`。非空值必须符合目录中的基数、属性、必填项和基本类型。`clauses` 至少包含一条，按数组顺序使用从 1 连续增长的 `order`，父条款必须先出现，页码不能超过处理版 PDF 总页数。

成功状态码：`201 Created`。

```json
{
  "status": "ingested",
  "document_id": "e7591f0d...64位哈希...",
  "file_name": "设备采购合同",
  "file_uri": "/e7591f0d...64位哈希....pdf",
  "page_count": 12,
  "ingestion": {
    "reviewer": "张三",
    "ingested_at": "2026-09-04T12:00:00+00:00"
  }
}
```

服务先在 `data/abstract/contracts.db` 中登记不可见的 `ingesting` 元数据，再幂等保存处理版 PDF，最后写入 `ELASTICSEARCH_INDEX_NAME` 指定的正式索引（默认 `contracts-v1`，不使用实验索引）。相同 `document_id` 会覆盖已有 ES 文档；只有 SQLite 转为 `ready`、文件和 ES 均核验成功后才删除运行并发布 `run.ingested`。持久化失败时 SQLite 记录失败原因并保留运行，调用方可以使用同一请求重试。

### 错误响应

| 状态码 | 条件 |
| --- | --- |
| `404` | `run_id` 不存在、已经过期、已经入库，或不属于当前审核用户。 |
| `409` | 尚有阶段未成功，或分类、Core、Clause、检索向量、页面向量等前置结果缺失。 |
| `422` | 文件名、完整 Core 或 Clause 不符合最终入库契约。 |
| `502` | SQLite、处理版 PDF 或 Elasticsearch 写入失败；运行仍保留，可重试入库。 |

更完整的校验、覆盖写入和失败边界见[复核后合同正式入库](../capability/application/contract-ingestion.md)。

---

## 推荐前端调用顺序

1. 使用审核用户密钥调用登录接口并保存 `login_code`。
2. 查询当前审核用户尚未入库的运行列表；用户选择已有 `run_id` 时查询其快照并重新建立 SSE，否则上传原始 PDF 创建新任务。
3. 后续请求统一携带 Bearer 免登码；新建任务时保存创建响应中的 `run_id`。
4. 立即渲染创建或同步响应中的 `contract_document_detection` 状态。
5. 使用支持 `Authorization` 请求头的客户端建立 SSE 连接，按事件更新处理时间线。
6. 收到 `run.document_rejected` 后展示非合同证据并停止流程；若合同识别成功，则继续等待查重事件。
7. 收到 `run.duplicate_rejected` 后继续渲染候选 PDF 列表，隐藏后续操作并停止流程；收到 `run.deduplication_review_required` 则展示相似候选并允许继续。预览统一将 `file_uri` 传给资源文件接口。
8. 在 `review_expires_at` 前完成与提取流无关的候选处理，然后调用一次 `POST .../{run_id}/continue`。
9. 继续消费合同结构识别、分类、建议名称和三个并行提取阶段；收到建议名称完成事件时初始化可编辑文件名，收到 `draft.updated` 或 `run.review_ready` 后获取一次全量快照。
10. 对 `retryable: true` 的失败阶段提供断点重试入口；阶段成功后继续消费后续 SSE，并在 `draft.updated` 后获取最新修订。
11. 用户放弃当前任务时调用 `DELETE .../{run_id}`；收到 `run.cancelled` 后关闭 SSE 并从界面移除任务。仅关闭 SSE 不会取消后台任务。
12. 八个阶段全部成功后，提交用户确认的最终 `file_name`、完整 Core 和 Clause 到入库接口。
13. 收到 `run.ingested` 或 `201 Created` 后关闭 SSE 并移除本地运行；服务端已经删除该 `run_id`。收到 `run.expired` 时关闭 SSE 并提示重新上传。
