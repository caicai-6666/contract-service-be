# vLLM 自定义聊天模板

> **用途：** 本文说明项目自有 Qwen3.6 多模态 chat template 的位置、工具布局契约、vLLM 启动方式和接入边界。图片重复传输与媒体缓存由独立的[vLLM 多模态媒体引用](vllm-media-reference.md)负责。

---

## 模板定位

模板位于 [`data/template/qwen3.6-tools-placement.jinja`](../../../data/template/qwen3.6-tools-placement.jinja)，以本地 `Qwen3.6-35B-A3B-FP8` 模型附带的 `chat_template.jinja` 为基线，保留其多模态占位符、system 约束、thinking 控制、assistant 工具调用和 tool response 历史格式，只改变请求工具定义的渲染位置。

vLLM 使用 Jinja chat template 将 OpenAI `messages`、`tools` 和特殊 token 转换为模型输入。官方服务支持通过 `--chat-template` 指定文件路径，并通过 `chat_template_kwargs` 向服务器拥有的模板传递扩展变量；本项目不启用客户端提交任意模板的 `--trust-request-chat-template`。完整接口见 [vLLM Chat Template](https://docs.vllm.ai/en/stable/serving/openai_compatible_server/#chat-template) 和 [vLLM Serve 参数](https://docs.vllm.ai/en/latest/cli/serve/)。

---

## 模型工具调用格式资产

当前 `.env` 与 `.env.example` 的生成模型为 `qwen3.6-35b-a3b-fp8`，对应独立提示词模板为 [`data/tool-tag/qwen3.6-35b-a3b-fp8.txt`](../../../data/tool-tag/qwen3.6-35b-a3b-fp8.txt)。当前资产版本为 `qwen3.6-tool-tag-v2`，仅在本文记录版本，不向模板正文增加文字。Embedding 模型不生成工具调用，因此不建立对应模板。

该文件直接复制 [`TOOL_CALL_XML_INSTRUCTION`](../../../app/agent/contract_extraction/tool_protocol.py) 的既有原文，不增加标题、规则或示例；与代码常量相比仅多文本文件的末尾换行。后续以 UTF-8 读取并移除一个末尾换行，即可得到与原常量完全相同的文本。它不是服务端 Jinja 模板，也不是工具 JSON Schema。

模板说明 XML 标签结构、必填参数和调用结束后不得追加文本的要求。单轮必须且只能调用一个工具等规则继续由节点任务提示词和程序校验承担；实际工具名称及参数定义通过 OpenAI `tools` 提供。提示词不能保证输出合法，仍需服务端 `qwen3_xml` 解析、客户端协议及业务校验和有限次数纠错。

使用 `VLLM_MLLM_TOOL_TAG_FILE=qwen3.6-35b-a3b-fp8.txt` 指定模板文件名，对应 `settings.mllm.tool_tag_file`。只能填写文件名，不接受绝对路径或子目录；路径固定解析到项目根目录的 `data/tool-tag`，不受启动工作目录影响，也不允许符号链接指向目录之外。未设置时使用上述默认值。

`app.bootstrap.lifespan` 在初始化数据库和外部客户端之前调用 `initialize_mllm_tool_tag(settings.mllm)`，以 UTF-8 读取并检查非空，将文本保存到 `app.core.tool_tag` 的进程级全局变量。文件缺失、不可读、编码错误或空白内容均阻止启动，不静默回退。运行期间不再读盘，修改文件或配置需要重启；每个进程独立加载。公共读取方式如下：

```python
from app.core.tool_tag import get_mllm_tool_tag

# 在启动初始化完成后、构造请求时读取，不要在模块导入期求值。
instruction = get_mllm_tool_tag()
```

初始化前调用 getter 会抛出 `RuntimeError`。全局字符串通过 getter 共享，避免 `from ... import 变量` 捕获初始化前的旧值。离线脚本不经过 FastAPI 时，需要自行先调用初始化函数。

**当前接入边界：** 已实现启动加载与全局读取入口；会话记忆的筛选规划提示词已通过 getter 将模板追加到 system 末尾，并已接入节点1模型循环；节点2仍为占位，尚未执行真实 vLLM 测试。其他现有工作流继续使用既有提示词及 `TOOL_CALL_XML_INSTRUCTION`，尚未迁移。后续接入时应替换对应重复格式说明，而不是叠加多份。资产已加入 Git 白名单并随后端镜像复制；若部署以已有卷挂载整个 `data`，镜像内的新文件会被挂载遮蔽，需要将模板同步至实际数据卷。新增其他模板时也需同步 Git 和 Docker 文件白名单。

修改时应同步核对聊天模板、共享协议说明和解析器约定，并更新资产版本。离线测试覆盖配置读取、文件名校验、原文一致性、启动前访问、文件异常、内存快照和启动失败边界；不代表已验证真实模型调用成功率。

---

## 工具布局契约

模板只接受两个 `tool_placement` 值；缺省时使用 `after_task`，未知值直接拒绝渲染：

| 值 | 渲染顺序 | 适用场景 |
| --- | --- | --- |
| `before_task` | 公共消息 → 工具 → 最后一个真实 user 任务 → 短期历史 | 并行任务共享同一工具、任务载荷不同，例如合同分类。 |
| `after_task` | 公共消息 → 最后一个真实 user 任务 → 工具 → 短期历史 | 工具根据当前任务定义动态生成，例如 Core 字段提取。 |

未指定锚点时，“最后一个真实 user 任务”是从消息尾部反向找到的最后一个 `role=user` 消息，assistant/tool 交互不改变它，但新增 user 纠错消息会改变。现在可通过 chat_template_kwargs 的 `tool_task_index` 指定初始 user 任务在 messages 中的零基索引；必须为有效整数且指向 user 消息，否则客户端或模板拒绝。会话记忆节点固定传入 1，保证工具块在纠错及工作区重建后仍位于初始任务前；其他未传此参数的工作流保持原有行为。修改此参数逻辑后须更新服务端模板并重启 vLLM。

`before_task` 要求调用方把公共前缀和任务变量拆为两个 user 消息，否则工具会位于同一个 user 消息全部内容之前：

```text
system：跨任务稳定规则
user：PDF + 文档结构 + 节点公共规则
user：当前类别定义与专家样例
```

`after_task` 允许当前定义与任务要求位于最后一个 user 消息中，模板随后追加动态工具：

```text
system：跨任务稳定规则
user：PDF + 文档结构 + 节点公共规则
user：当前字段定义
tools：根据该字段生成的 non-strict Schema（本地继续严格校验）
assistant/tool：短期记忆
```

> **控制边界：** `tool_placement` 必须由应用节点以受限枚举传入，不得扫描合同文本或普通用户指令决定布局。模板仍由 vLLM 服务端统一持有，模型只能看到渲染后的 token 序列。

---

## 启动方式

从项目根目录启动 vLLM 时显式指定模板，并保留 OpenAI 多模态内容格式：

```bash
vllm serve /root/autodl-tmp/model/Qwen3.6-35B-A3B-FP8 \
    --trust-remote-code \
    --quantization fp8 \
    --gpu-memory-utilization 0.58 \
    --max-model-len 262144 \
    --max-num-seqs 512 \
    --enable-prefix-caching \
    --enable-auto-tool-choice \
    --tool-call-parser qwen3_xml \
    --host 0.0.0.0 \
    --port 8000 \
    --served-model-name qwen3.6-35b-a3b-fp8 \
    --chat-template data/template/qwen3.6-tools-placement.jinja \
    --chat-template-content-format openai \
    --structured-outputs-config '{"backend":"xgrammar"}'
```

修改模板后必须重启 vLLM。应用继续通过 OpenAI `tools`、`tool_choice` 和 `parallel_tool_calls` 传递机器工具契约；模板只控制模型看到的 token 顺序，不替代 vLLM 的 structured outputs 或项目 Pydantic 二次校验。

调用方通过 OpenAI 兼容接口的 `chat_template_kwargs` 传入布局变量；使用 Python SDK 时应放在 `extra_body` 中，而不是写入普通 user 消息：

```python
await client.chat.completions.create(
    model=model_name,
    messages=messages,
    tools=tools,
    extra_body={
        "chat_template_kwargs": {"tool_placement": "after_task"},
    },
)
```

---

## 当前接入状态与验证

模板文件及离线顺序测试已经建立，`MLLMClient.create_tool_chat_completion` 已支持可选的受限 `tool_placement` 参数；文档结构发现节点显式使用 `after_task`。分类正式判定建立公共 user 与任务 user 的边界并显式使用 `before_task`，因此全部类别请求共享“公共前缀 + 同一工具块”，只在最后的类别资料处发生分叉。首个视觉请求填充 vLLM 媒体缓存后，并发分类只发送相同页面的 UUID 引用；模型侧继续按相同内容建立 prefix cache，不需要独立预热请求。

当前离线验证覆盖：

- `before_task` 将工具稳定放在公共 user 消息之后、任务 user 消息之前。
- `after_task` 将工具放在任务内容之后、第一轮 assistant 生成之前。
- 多轮 assistant/tool 历史不会使工具块移动或重复。
- 非法 `tool_placement` 会在模板渲染阶段失败。

正式启用前还必须使用真实 vLLM 对照验证工具调用成功率、strict 参数首次通过率、重试轮数、prompt/cached token 和首 token 延迟。缓存指标不能替代分类或提取准确性验证。
