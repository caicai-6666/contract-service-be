# vLLM 自定义聊天模板

> **用途：** 本文说明项目自有 Qwen3.6 多模态 chat template 的位置、工具布局契约、vLLM 启动方式和接入边界。图片重复传输与媒体缓存由独立的[vLLM 多模态媒体引用](vllm-media-reference.md)负责。

---

## 模板定位

模板位于 [`data/template/qwen3.6-tools-placement.jinja`](../../../data/template/qwen3.6-tools-placement.jinja)，以本地 `Qwen3.6-35B-A3B-FP8` 模型附带的 `chat_template.jinja` 为基线，保留其多模态占位符、system 约束、thinking 控制、assistant 工具调用和 tool response 历史格式，只改变请求工具定义的渲染位置。

vLLM 使用 Jinja chat template 将 OpenAI `messages`、`tools` 和特殊 token 转换为模型输入。官方服务支持通过 `--chat-template` 指定文件路径，并通过 `chat_template_kwargs` 向服务器拥有的模板传递扩展变量；本项目不启用客户端提交任意模板的 `--trust-request-chat-template`。完整接口见 [vLLM Chat Template](https://docs.vllm.ai/en/stable/serving/openai_compatible_server/#chat-template) 和 [vLLM Serve 参数](https://docs.vllm.ai/en/latest/cli/serve/)。

---

## 工具布局契约

模板只接受两个 `tool_placement` 值；缺省时使用 `after_task`，未知值直接拒绝渲染：

| 值 | 渲染顺序 | 适用场景 |
| --- | --- | --- |
| `before_task` | 公共消息 → 工具 → 最后一个真实 user 任务 → 短期历史 | 并行任务共享同一工具、任务载荷不同，例如合同分类。 |
| `after_task` | 公共消息 → 最后一个真实 user 任务 → 工具 → 短期历史 | 工具根据当前任务定义动态生成，例如 Core 字段提取。 |

“最后一个真实 user 任务”是从消息尾部反向找到的最后一个 `role=user` 消息；后续 `assistant` 工具调用和 `role=tool` 反馈不改变这个锚点。因此同一任务多轮调用时，工具块始终固定在初始任务与短期历史之间，只渲染一次。

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
