# 外部专家求助工具

## 用途与实现范围

向外部专家提出专业问题，例如公司信息核实、交易规则和条款分析。只传文字，不承担图片识别。当前已实现参数模型、工具定义、提示词拼接、DeepSeek 配置、异步客户端、多轮专家会话池及主循环注册。完整业务门禁通过后，主助手可发起求助及继续追问。

代码位于 `app/agent/contract_communication/agent_core/tool/external_expert.py`，依赖 Pydantic。接入遵循[工具执行契约](tool-execution.md)和[多轮上下文规范](../../../standard/agent-context-management.md)。

---

## 参数与提示词

`AskExternalExpertArguments` 对应新建工具 `ask_external_expert`，`ContinueExternalExpertArguments` 对应追问工具 `continue_external_expert`。`build_external_expert_tools()` 返回两个带字段说明的函数工具 Schema。未知属性、空白文本和非文字值均拒绝。

| 工具 | 参数 | 契约 |
| --- | --- | --- |
| `ask_external_expert` | `question` | 必填完整首轮消息，包含专业问题与必要原文、背景 |
| `ask_external_expert` | `system_prompt` | 可选职责；省略或 null 使用默认职责，创建后固定 |
| `continue_external_expert` | `session_id` | 必填，先前成功返回的完整专家会话标识 |
| `continue_external_expert` | `question` | 必填本轮消息，可直接包含追问、新增事实、更正或补充回答；历史由程序维护 |

新建工具不接受 `session_id` 或 `context`，追问工具不接受 `context` 或 `system_prompt`（包括显式 null），通过两个独立模型的 `extra=forbid` 校验。

`build_external_expert_system_prompt(system_prompt=None)` 返回固定共同规则与专家职责拼接后的完整 system 文本。版本为 `external-expert-v10`。固定规则为稳定前缀，不能通过参数删除；规则要求区分事实与判断、提供实际来源、说明信息缺失和时效限制，不承诺未配置的检索能力。冲突的自定义要求应让位于共同规则；这是提示词约束，并非程序已能判定任意自定义文字的语义合法性。

发起工具的总体描述由 `AskExternalExpertArguments` 类 docstring 提供，覆盖适合求助的情形、专家可见信息边界、问题与事实材料组织、新建/追问选择、缺失信息处理和答复使用原则。字段描述同时约束材料的来源与表达。规则随实际工具定义注入，不在 Agent Core 系统提示词中另建专家专属章节，也不将提示词描述误当作程序已经能核验资料真实性。

主助手的编写指导集中在 `EXPERT_PROMPT_AUTHORING_GUIDANCE`：按需定义职责范围、分析维度与方法、证据与时效标准、缺失/冲突处理、交付要求，包含条款分析和公司核实两个示例；这些示例描述工作方式，不注入具体待判事实或预设结论。指导由 `system_prompt` 的 `Field(description=EXPERT_PROMPT_AUTHORING_GUIDANCE)` 引用，只出现在该字段的实际 JSON Schema 中，不重复追加至工具总体描述。总体描述负责调用时机、问题与背景组织、会话选择及答复使用；追问工具只说明继续原会话的规则。工具通过统一注册表注入主助手上下文。具体问题与资料作为独立求助正文，不拼进 system 段；两个工具统一以 `question` 承载本轮文字消息；首次在其中说明问题和必要背景，后续只提交追问或新增信息。程序自动携带已成功提交的历史，不要求模型维护独立 context。

```python
from app.agent.contract_communication.agent_core.tool.external_expert import (
    AskExternalExpertArguments, build_external_expert_system_prompt,
)

request = AskExternalExpertArguments(
    question="该条款有哪些适用条件和争议风险？条款原文与已知交易背景……",
)
system = build_external_expert_system_prompt(request.system_prompt)
```

---

## DeepSeek 配置

`.env` 与 `.env.example` 已提供下列变量，通过 `get_settings().deepseek` 读取。API Key 允许为空，不影响现有服务；使用 `SecretStr` 避免配置常规展示泄露密钥。创建 `DeepSeekClient` 时校验凭据，空值直接拒绝，不发起请求。

| 环境变量 | 默认值 |
| --- | --- |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` |
| `DEEPSEEK_API_KEY` | 空 |
| `DEEPSEEK_MODEL` | `deepseek-v4-pro` |
| `DEEPSEEK_REASONING_EFFORT` | `high` |
| `DEEPSEEK_TIMEOUT_SECONDS` | 300 |
| `DEEPSEEK_MAX_CONCURRENT_REQUESTS` | 3 |
| `DEEPSEEK_MAX_COMPLETION_TOKENS` | 8192 |

`DEEPSEEK_REASONING_EFFORT` 仅接受 `none`、`low`、`high`、`max`，非法值在配置加载时拒绝。Responses 请求传入 `reasoning={"effort": 配置值}`；none 关闭思考，其余开启，不额外发送 Chat Completions 专用的 thinking 字段。修改后重启生效；本次未调整输出 token 预算。参数依据 [DeepSeek 思考模式文档](https://api-docs.deepseek.com/guides/thinking_mode/)。

地址和模型名依据 [DeepSeek 官方 API 文档](https://api-docs.deepseek.com/api/create-chat-completion/)；模型可替换。超时、并发及输出预算为项目初始选择，不表示厂商推荐值；客户端发送请求时使用这些限制。配置独立于本地 vLLM，不复用其工具模板或专用采样参数。修改环境配置后需重启以刷新缓存。复用已安装的 OpenAI SDK，每轮显式提供内置 web_search，专家按问题需要执行搜索；不强制每轮搜索，也不将工具可用视为事实已核实。

---

## 验证与后续边界

离线检查覆盖默认与自定义职责拼接、固定前缀一致性、空白/未知参数拒绝、实际工具 Schema 的字段描述及配置加载和密钥遮蔽。真实专业回答与联网检索已进行小样本实验；主循环恢复流程由离线联调覆盖，实验结果不代表所有引用均准确。


---

## 异步客户端

`app/infrastructure/deepseek.py` 提供 `DeepSeekClient`，通过 `AsyncOpenAI.responses.create` 异步调用。客户端不保存消息历史、不拼接专家提示词；调用方每次传入完整 `input_items`，包含文字消息以及先前成功响应中的 message、reasoning、web_search_call 项目。每轮固定注入 `tools=[{"type": "web_search"}]`，无需模型传工具开关；不提供 previous_response_id 或依赖服务端保存历史。图片、客户端函数工具及空白输入不在本客户端范围。

```python
from app.core.config import get_settings
from app.infrastructure.deepseek import DeepSeekClient

async with DeepSeekClient(get_settings().deepseek) as client:
    result = await client.create_response(input_items=[
        {"role": "system", "content": system},
        {"role": "user", "content": request.question},
    ])
    if not result.is_complete:
        # 输出达到上限：由工具执行器处理，不当成完整专家答案。
        raise RuntimeError("专家答复尚未完成")
```

示例只演示客户端；多轮求助应通过下述会话池组织本轮消息和历史。连接在实例内复用，`async with` 或 `await close()` 关闭资源。允许注入测试用 `httpx.AsyncClient`，其关闭责任转移给本客户端。调用方应先结束或取消在途请求，再关闭连接。

所有实例通过 `model_concurrency` 共享独立的 deepseek 配额，不占用 mllm 或 embedding 额度。SDK 自动重试关闭，避免隐式重复外部调用；是否重试由调用方决定。取消保持原样传播并释放配额。请求超时来自配置，排队等待不计入 SDK 网络超时。

返回 `DeepSeekCompletion`，包含正文、响应标识、模型名、token 用量、Responses 状态（保存在 finish_reason 字段）、供历史回传的 output_items 及供私有审计使用的原始响应。`finish_reason=incomplete` 允许保留空正文或部分正文，但 `is_complete=False`；不得向用户声明已取得完整结论。客户端函数调用、拒答、无有效正文或异常结束均失败。内置搜索调用由服务端执行，网页打开失败可与完整文字答复同时存在，原始输出保留其失败状态，不能伪装访问成功。原始响应不应直接注入主助手轨迹。

连接/超时、HTTP 408/409/429 与 5xx 转换为 `DeepSeekUnavailableError`；配置、输入、响应契约及其他 HTTP 错误为 `DeepSeekRequestError`。面向调用方的错误文本不复制远端响应正文。无正文的请求指标接入现有 `inference_metrics`，该层不自动记录完整问题与答复。

离线验证使用真实 AsyncOpenAI 与模拟 HTTP transport，覆盖请求体、用量、无状态消息、资源关闭、空密钥与参数拒绝、错误分类、无自动重试、超时、截断、跨实例限流和取消释放；未进行真实远端调用。


---

## 多轮专家会话池

`tool/external_expert.py` 集中定义提示词、参数模型、答复渲染器、`ExternalExpertSessionPool` 与两个工具的注册入口。会话池继续作为独立类管理驻留和多轮历史；底层 API 通信仍由 `infrastructure/deepseek.py` 负责。这是主助手与专家之间的多轮消息会话，不是远端持久 session，也不是已经实现的标准 A2A 网络协议。每个宿主用户会话持有独立实例，注入无状态 `DeepSeekClient`；实例不得跨用户会话共享。身份由宿主绑定，不接受模型指定用户或宿主会话。

```python
from app.agent.contract_communication.agent_core.tool import (
    ExternalExpertSessionPool, ContinueExternalExpertArguments,
)

# client 由宿主持有；pool.close() 只释放对话，不关闭共享连接。
pool = ExternalExpertSessionPool(conversation_id, client, capacity=10)
first = await pool.ask(AskExternalExpertArguments(question="请评估该条款的适用条件。条款原文……"))
if first["status"] == "success":
    followup = await pool.continue_conversation(ContinueExternalExpertArguments(
        session_id=first["session_id"], question="若交易主体变为自然人，哪些判断需要调整？",
    ))
pool.close()
```

### 对话与提交

创建时固定“共同规则＋默认/自定义职责”。成功返回 `session_id`、`round` 和已渲染的专家答复 `content`；后续通过 `continue_external_expert` 使用原始完整标识，不能中途改写 system prompt，调整职责需要用 `ask_external_expert` 新建会话。池分别提供 `ask()` 和 `continue_conversation()`，内部共用历史管理与原子提交逻辑。

每轮发送 system、此前成功的 user/assistant 消息、当前问题与背景。只有完整答复通过校验才同时提交当前 user 与 assistant；失败、取消、截断以及容量超限均不提交半轮，不污染后续上下文。普通专家文字是本工具正式结果，不需要强制模拟函数调用。专家内部历史保留成功轮的原生输出项目及顺序，包括 reasoning、web_search_call、message；推理与搜索项目回传给专家，不注入主助手的普通答复正文。

可注入 `audit(event)` 记录本轮消息、原始答复和接受状态；错误和被丢弃的响应同样记录，正常历史清理不清除审计。宿主负责配置审计存储和访问边界，默认未配置审计落盘。池不自动重试或生成伪造纠错轨迹，错误由外层工具执行器反馈主助手。

### 容量与生命周期

默认驻留 10 个专家会话，LRU 驱逐；`capacity` 可在构造时配置。新建请求开始前预留名额，可能驱逐最久未访问的空闲会话，即使新请求随后失败，被驱逐会话也不会恢复。全部会话在途时返回 `pool_busy`。同一专家会话在途时返回 `session_busy`，不同专家会话可并发，受 DeepSeek 共享请求配额限制。

每会话默认最多 20 个成功问答轮次、100000 个历史序列化字符，通过 `max_rounds`、`max_history_chars` 配置。字符限制按 ensure_ascii=False 的 JSON 序列化长度计算，包含 system、历史原生项目及本轮请求/答复，不是 tokenizer 精确预算；是防止本地历史无界增长的保护，不能保证任意服务端上下文都不超限。达到限制直接反馈，不自动删减或摘要历史，也不冒充无限续聊。

错误反馈含 `status=error`、`error_code`、`message`、可继续使用的 `session_id`。已驱逐/不存在时明确要求新建并补齐背景，不能静默新建并假装记得历史。新建失败会撤销临时会话，返回 null 标识；已存在会话失败保留原历史。

`close()` 释放所有专家历史并永久关闭池；迟到响应不得重新写回。池不自行取消调用方持有的任务或关闭共享客户端。宿主会话驱逐、删除时同步关闭池以撤销提交资格，并取消在途任务；服务停止先取消并等待生产者，再释放池和共享客户端。当前不落盘，重启后无法续用旧标识。

离线测试覆盖多轮原文传递、职责冻结、跨池隔离、LRU、请求失败和截断清理、在途保护、释放后的迟到响应、取消释放名额及轮数/字符限制。真实专家多轮已完成小样本验证；主循环闭环已增加离线联调。


---

## 成功答复渲染

两个池入口成功时共用 `render_external_expert_reply(session_id=..., round_number=..., content=...)`，版本 `external-expert-render-v1`。成功返回的 `content` 为下列文本，结构化 `session_id` 与 `round` 同时保留，便于程序使用。错误反馈不套成功包络。

```text
════════════ 外部专家答复 ════════════
专家会话：完整 session_id
对话轮次：2

以下内容为外部专家意见，需结合其依据、适用条件和不确定性判断。

〈专家答复正文，原样呈现〉

────────────────────────────────
如需追问或补充背景，请调用 continue_external_expert，
并使用上方完整的专家会话标识。
════════════ 专家答复结束 ════════════
```

正文逐字保留，不强制二次拆分、提取或改写栏目。专家自身会话历史保留原生输出项目，不回注面向主助手的标题、来源提醒和追问提示。工具回执按 `ordinary` 类型接入，不使用页面折叠；两个工具均经统一注册表、FIFO 管理及 Executor 执行。


---

## Anthropic 兼容接口搜索实验

项目环境安装 `anthropic` SDK，依赖记录在 `requirements.txt`。目前仅用于 `experiment/deepseek-web-search/run.py` 的异步协议对照，生产专家客户端已切换至 AsyncOpenAI Responses；Anthropic SDK 仍仅用于实验对照。

实验分别探测 Anthropic `web_search_20250305`、Chat Completions `web_search` 和 Responses `web_search`。工具格式参照 [Anthropic 搜索工具文档](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool)与 [DeepSeek 兼容说明](https://api-docs.deepseek.com/guides/anthropic_api/)；具体可用性以运行目录的原始响应与人工分析为准。实验只使用公开问题，结果需区分服务端实际搜索、工具错误与纯文字生成。


2026-09-16 实测（`experiment/deepseek-web-search/output/20260916T114028.579251Z`）：Anthropic 返回一次服务端搜索、十条结果并完成答复；OpenAI Responses 同样返回三次成功搜索并完成答复，但四次打开页面失败；Chat Completions 对 web_search 类型返回 400。Responses 实际行为与兼容文档的 ignored 声明不一致，后续已通过生产客户端与池进行多轮协议验证，原始记录见 run_pool.py 输出目录。官方 [9 月 10 日公告](https://www.deepseek.com/news/deepseek-v4-1-flash/)说明 9 月 14 日起 deepseek-v4-pro 别名路由至 V4.1 Flash，本实验不能作为原始 V4 Pro 权重能力结论。


当前生产类联调记录：`experiment/deepseek-web-search/output/20260916T115034.672460Z`。首次和追问均成功；第二轮完整回传第一轮原生 output，并继续触发搜索。主助手工具注册现已接入。Anthropic 仅保留为对照依赖，当前默认路径为 AsyncOpenAI Responses。


---

## 主循环装配与生命周期

`build_external_expert_registrations(get_pool=...)` 以已有参数模型和类说明注册 `ask_external_expert`、`continue_external_expert`，统一执行参数兼容解析、字段校验、FIFO 管理和错误恢复。成功映射为 `FIFOExecutionResult(status=succeeded)`，池返回的业务错误映射为 `failed`；原始推理、网页动作与审计不进入主助手工具回执。

`CommunicationWorkflowService` 在完整业务门禁放行后添加两个注册项。`bootstrap` 注入 `settings.deepseek`；首次实际求助才创建共享异步客户端，每个宿主会话独立持有专家池，跨用户任务复用同一宿主池。模型不能指定宿主会话身份。缺少 API key 时返回普通错误工具反馈，其他工具和主循环仍可继续运行。

两个工具均注册 `external-expert` 进度，执行期间展示“正在咨询外部专家：本轮提问内容”，由已校验的 `question` 动态生成；首问和追问均展示本轮问题。状态中的换行与连续空白合并为单个空格，总长超过 SSE 的2000字符上限时以省略号截断，仅影响展示，发送给专家的原始问题保持完整，结束后恢复“正在思考”。该状态表示当前执行步骤，不保证专家每次都实际搜索。专家正文不会直接作为 SSE 用户答案；主模型通过既有中途输出或最终输出工具交付用户。

成功/失败请求的完整私有审计暂存于 `_expert_audits[conversation_id]`，随宿主驱逐或服务关闭释放，不写入任务或数据库。池随宿主删除、驱逐释放；服务关闭同时关闭共享客户端。专家历史不持久化，重启后旧 session_id 失效，需重新求助。

离线验证见 `tests/test_agent_core_runtime.py`：正式门禁后装配、发起与追问、原生历史复用、普通结果回注、进度事件、跨会话隔离、池驱逐、客户端关闭，以及配置缺失后主模型继续执行并清理失败轨迹。客户端和池的独立测试继续覆盖超时、截断、取消、LRU 和迟到结果。


---

## 求助正文的提问特点

两个工具的 `question` 指导直接写在各自 `Field(description=...)`，经注册表进入模型可见 JSON Schema，不追加到 Agent Core 全局提示词。版本 `external-expert-v10` 调整提问与追问指导，不改变专家固定 system、参数类型或历史携带机制。

首次提问围绕单一核心疑点，按需提供目标、必要原文与事实、缺失或冲突信息、所需依据与结果，不要求填满固定模板。区分用户陈述、资料记载、已核实事实及主助手假设；不在背景、括号或风险示例中偷带预设结论，不把近似概念直接等同。允许专家否定前提、修正假设；外部核验要求实际来源 URL、日期和核实边界，无法取得时明确限制。

追问聚焦影响当前任务的缺口，支持同任务内主动核查缺少依据、来源 URL、条件或存在冲突的答复，无需等用户再次发问；不强制每次求助后追问。用户更正时说明原条件、新条件及不变条件，程序仍自动携带成功历史，模型不维护独立 context。

这属于模型提问行为约束，程序仅校验非空文字及参数结构，不宣称可自动识别全部诱导性问题。实际效果需通过新的真实联调验证，既有实验不作为本版本效果证明。

---

## 驻留容量配置

`COMMUNICATION_EXPERT_SESSION_CACHE_MAX_SESSIONS` 控制每个会话专家会话池容量（默认10）。均通过 Settings 与环境变量加载，要求正整数；修改后重启服务生效。分页工具定义按实际页大小生成，不改变已有页码语义。
