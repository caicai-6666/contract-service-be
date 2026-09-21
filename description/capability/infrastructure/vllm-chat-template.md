# vLLM 自定义聊天模板

> **用途：** 本文说明项目自有 Qwen3.8-Flash-Next 多模态 chat template 的位置、工具布局契约、vLLM 启动方式和接入边界。图片重复传输与媒体缓存由独立的[vLLM 多模态媒体引用](vllm-media-reference.md)负责。

DeepSeek V4.1 Flash 已支持相同的 before_task/after_task 与 tool_task_index 扩展，另保留 system 布局；详见 [V4.1 模板说明](deepseek-v41-template.md)。GLM-5.3-Flash 同类适配见 [GLM 模板](glm53-template.md)。下文模板默认值、编码细节与启动示例针对 Qwen。

---

## 模板定位

模板位于 [`data/template/qwen3.8-tools-placement.jinja`](../../../data/template/qwen3.8-tools-placement.jinja)，当前用于 `Qwen3.8-Flash-Next-NVFP4`。模板最初以 `Qwen3.6-35B-A3B-FP8` 附带的 `chat_template.jinja` 为基线，当前版本 `qwen3.8-tools-placement-v2` 保留多模态占位符、工具前后置布局及历史格式，并按照 Qwen3.8 官方模板补齐推理强度指令。

vLLM 使用 Jinja chat template 将 OpenAI `messages`、`tools` 和特殊 token 转换为模型输入。官方服务支持通过 `--chat-template` 指定文件路径，并通过 `chat_template_kwargs` 向服务器拥有的模板传递扩展变量；本项目不启用客户端提交任意模板的 `--trust-request-chat-template`。完整接口见 [vLLM Chat Template](https://docs.vllm.ai/en/stable/serving/openai_compatible_server/#chat-template) 和 [vLLM Serve 参数](https://docs.vllm.ai/en/latest/cli/serve/)。

---

## 模型工具调用格式资产

当前 `.env` 与 `.env.example` 的生成模型为 `qwen38-flash-next`，对应独立提示词模板为 [`data/tool-tag/qwen3.8-flash-next-nvfp4.txt`](../../../data/tool-tag/qwen3.8-flash-next-nvfp4.txt)。当前资产版本为 `qwen3.8-tool-tag-v1`，仅在本文记录版本，不向模板正文增加文字。Embedding 模型不生成工具调用，因此不建立对应模板。

该文件直接复制 [`TOOL_CALL_XML_INSTRUCTION`](../../../app/agent/contract_extraction/tool_protocol.py) 的既有原文，不增加标题、规则或示例；与代码常量相比仅多文本文件的末尾换行。后续以 UTF-8 读取并移除一个末尾换行，即可得到与原常量完全相同的文本。它不是服务端 Jinja 模板，也不是工具 JSON Schema。

模板说明 XML 标签结构、必填参数和调用结束后不得追加文本的要求。单轮必须且只能调用一个工具等规则继续由节点任务提示词和程序校验承担；实际工具名称及参数定义通过 OpenAI `tools` 提供。提示词不能保证输出合法，仍需服务端 `qwen3_xml` 解析、客户端协议及业务校验和有限次数纠错。

使用 `VLLM_MLLM_TOOL_TAG_FILE=qwen3.8-flash-next-nvfp4.txt` 指定模板文件名，对应 `settings.mllm.tool_tag_file`。只能填写文件名，不接受绝对路径或子目录；路径固定解析到项目根目录的 `data/tool-tag`，不受启动工作目录影响，也不允许符号链接指向目录之外。未设置时使用上述默认值。

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

以下同步当前部署命令；从项目根目录执行，并将应用的 `VLLM_MLLM_BASE_URL` 指向 `http://127.0.0.1:6006/v1`。远程部署需同步模板文件并改为实际绝对路径；本次文件更名不自动替换远端文件或重启 vLLM：

```bash
conda activate vllm-new
export OMP_NUM_THREADS=4

CUDA_VISIBLE_DEVICES=0 vllm serve /root/autodl-tmp/model/Qwen3.8-Flash-Next-NVFP4 \
    --served-model-name qwen38-flash-next \
    --host 127.0.0.1 \
    --port 6006 \
    --tensor-parallel-size 1 \
    --quantization modelopt \
    --engram-config '{"cpu_offload":true}' \
    --gpu-memory-utilization 0.99 \
    --max-model-len 262144 \
    --max-num-seqs 16 \
    --max-num-batched-tokens 4096 \
    --compilation-config '{"cudagraph_mode":"FULL_AND_PIECEWISE"}' \
    --speculative-config '{"method":"mtp","num_speculative_tokens":3}' \
    --enable-prefix-caching \
    --reasoning-parser qwen3 \
    --enable-auto-tool-choice \
    --tool-call-parser qwen3_xml \
    --chat-template data/template/qwen3.8-tools-placement.jinja
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


---

## 全局推理强度

`VLLM_MLLM_REASONING_EFFORT=xhigh` 对应 `settings.mllm.generation.reasoning_effort`，作为本地 vLLM MLLM 节点的默认强度；合同提取全链路使用独立的 `VLLM_MLLM_EXTRACTION_REASONING_EFFORT`，详见[提取配置](../../architecture/workflow/contract-extraction/readme.md#原生思考配置)。会话业务门禁使用独立的 `VLLM_MLLM_BUSINESS_GATE_REASONING_EFFORT`，默认 `xhigh`，详见[门禁配置](../../architecture/workflow/contract-communication/business-gate.md#原生思考与输出预算)。普通文本、强制 JSON、工具调用（含流式调用）都从所属流程的配置副本读取；客户端不再接受单次调用覆盖 reasoning_effort。FIFO 摘要两个节点也不再固定 xhigh，其审计记录实际配置值。外部 DeepSeek 专家继续使用独立的 DEEPSEEK_REASONING_EFFORT。

各节点的 enable_thinking 仍决定是否开启思考；关闭时不发送强度，开启时经 chat_template_kwargs 传入统一值。聊天 token 计数使用同一配置，避免遗漏模板插入的指令长度。

| 模板 | 可选值 | 行为 |
| --- | --- | --- |
| Qwen3.8 | low、medium、xhigh | 原样传入；low/xhigh 向初始 system 加入官方强度指令，medium 不增加指令。 |
| DeepSeek V4.1 | low、medium、xhigh | 分别转换为 50、75、100，兼容原生编码器与项目 Jinja。 |
| GLM-5.3-Flash | low、medium、xhigh | 分别转换为 low、high、max；不支持通过项目模板关闭思考，详见 GLM 专题。 |

配置层只接受统一三档，默认 xhigh；整数、high、max 等模型专属值不再作为环境配置接受。MLLMSettings.thinking_template_kwargs 是生成与分词共用的转换入口，以 VLLM_MLLM_TOOL_TAG_FILE=deepseek-v4.1-flash.txt 明确选择 DS 协议，不根据可任意命名的 served-model-name 猜测模型。glm-5.3-flash.txt 选择 GLM 映射，其他工具格式沿用三档原值。更换模型时同步选择匹配的 tool-tag，推理强度配置无须改变。

DS Jinja 自身也接受统一三档，便于直接模板调用；请求层提前转换，是为了兼容绕过 Jinja 的原生编码器。三档只统一使用意图，不承诺不同模型相同的推理 token 消耗。Qwen 定义依据[官方模板](https://huggingface.co/Qwen/Qwen3.8-Flash-Next/blob/main/chat_template.jinja)。

修改环境变量后需重启后端。首次部署本次 Qwen 模板更新，还需同步 Jinja 到模型服务器并重启 vLLM；旧模板不消费 reasoning_effort。当前只完成代码及离线验证，没有重启运行中的服务。

验证见 `tests/test_mllm_reasoning_effort.py`：配置校验、两种模型三类生成请求的开关/档位、DS 三档数值转换与分词一致性、Qwen 三档与两种工具布局。关闭思考不代表所有节点都关闭：显式开启思考的专用节点继续使用所属流程的推理强度。
