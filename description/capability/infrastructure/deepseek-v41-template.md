# DeepSeek V4.1 Flash 模板

> **用途：** 提供项目维护的 DeepSeek V4.1 Flash Jinja 消息模板和工具格式提示词。当前仅新增可选资产，不切换运行模型，不修改现有 Qwen 配置。

---

## 文件与依据

| 文件 | 职责 |
| --- | --- |
| [deepseek-v4.1-flash.jinja](../../../data/template/deepseek-v4.1-flash.jinja) | 将消息、工具定义和工具历史渲染为 V4.1 原生文本协议。版本 `deepseek-v4.1-flash-template-v3`。 |
| [deepseek-v4.1-flash.txt](../../../data/tool-tag/deepseek-v4.1-flash.txt) | 注入节点提示词的工具调用格式说明。版本 `deepseek-v4.1-tool-tag-v1`，版本不写入正文。 |

官方编码器及所复用工具说明的 MIT 许可保留在 Jinja 文件末尾注释中，不进入模型输入。

依据官方 [编码说明](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/blob/main/encoding/README.md) 和 [encoding.py](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/blob/main/encoding/encoding.py) 编写。官方提供 Python 编码器，本文件是项目对其常用消息路径的 Jinja 适配，不是官方发布的 Jinja。2026-09-20 核对的编码器 SHA-256 为 `502bdaec8a3fd88ebc24c4721a7038fbe42f2063c664638127056107920035c1`。

---

## 消息与工具契约

模板支持 system、user、assistant、tool 和 latest_reminder；使用原生 BOS、角色标记、思考分隔符和 assistant EOS。连续 user/tool 消息合并，工具结果按此前调用 ID 的顺序排列。历史工具 arguments 必须预先转换为字典；JSON 字符串会明确报错，不能被错误渲染为额外的 arguments 参数。

工具 Schema 继续由请求 tools 注入，不在 tool-tag 中重复定义具体工具。模板支持与 Qwen 相同的任务相对位置扩展，并保留官方英文工具指令：

| tool_placement | 渲染位置 |
| --- | --- |
| after_task（默认） | 公共消息 → 初始任务完整内容（含图片）→ 工具定义 → 后续轨迹。 |
| before_task | 公共消息 → 工具定义 → 初始任务 → 后续轨迹。 |
| system | 工具留在 system 区，保留官方编码布局，用于兼容与对照。 |

`tool_task_index` 为原始 messages 中 user 消息的零基索引；未指定时定位最后一个真实 user，排除整段 tool_response 包装的工具反馈文本。消息合并时保留原始索引，避免连续 user/tool 合并影响锚点。指定非整数、布尔值、越界值或非 user 消息时报错。新增纠错 user 会影响默认锚点，多轮节点应显式传入初始任务索引。

工具块只注入一次，前后置模式不在 system 重复输出；无工具时不新增工具内容，但仍校验显式位置参数。任务前后置模式也支持首条 system 上携带的 tools；不接受会话中 system 携带额外工具定义，避免重复或静默遗漏。工具模式原有历史思考保留规则不受位置改变影响。

```python
extra_body = {
    "chat_template_kwargs": {
        "enable_thinking": True,
        "reasoning_effort": "medium",
        "tool_placement": "before_task",
        "tool_task_index": 1,
    }
}
```

前后置是项目扩展，将工具块置于锚点 user 内容内部，并非官方推荐布局；模型侧效果仍需真实推理验证。客户端现有 before_task/after_task 参数已能传递，无需新增业务节点参数。system 为模板直接调用选项，应用客户端现有布局枚举保持两种任务位置。

V4.1 标签中的空格属于协议：

```text
<｜DSML｜ calls>
<｜DSML｜ invoke name="lookup">
<｜DSML｜ parameter name="query" string="true">合同</｜DSML｜ parameter>
<｜DSML｜ parameter name="page" string="false">1</｜DSML｜ parameter>
</｜DSML｜ invoke>
</｜DSML｜ calls>
```

字符串直接保留原文，其他参数使用 JSON，包括 null、布尔值及嵌套对象。单轮工具数量限制仍由各节点提示词和程序校验承担。

内容支持文本和图像块，图像仅渲染 `<｜deepseek_image｜>` 占位符；图片载入、切片与视觉 token 扩展需要服务端配套的多模态处理器。本模板不支持视频、音频、工具命名空间、内部 task 标记及消息级 response_format/content_blocks 扩展，遇到这些输入明确拒绝。API 的结构化输出仍由服务端实现，不能通过本模板代替。

---

## 思考与历史控制

| 参数 | 含义 |
| --- | --- |
| `enable_thinking` | 默认 true；false 使用 chat 模式。 |
| `thinking_mode` | 可显式指定 thinking/chat，优先于 enable_thinking。 |
| `reasoning_effort` | 统一三档 low=50、medium=75、xhigh=100；模板兼容官方整数 1–100 及 high=75、max=100，未传参数时保持官方默认 high=75。越界数值及布尔值拒绝。 |
| `drop_thinking` | 默认 true；普通会话移除最后一个用户段之前的思考。只要存在工具定义，按官方规则保留传入的思考。 |
| `add_generation_prompt` | 默认 true；false 不为末尾用户段添加待生成的 assistant 头。 |

历史思考读取 reasoning_content，兼容 reasoning。模板只能保留请求实际提供的内容，不扩大 Agent Core 自身的思考 FIFO 窗口。应用统一通过 VLLM_MLLM_REASONING_EFFORT 配置强度，环境变量只接受 low/medium/xhigh，默认 xhigh；节点不能单独覆盖。选择 DS tool-tag 后，生成与分词请求统一转换为 50/75/100，兼容绕过 Jinja 的原生编码器，详细契约见[全局推理强度](vllm-chat-template.md#全局推理强度)。

---

## 使用与部署边界

应用端切换工具提示词时设置并重启后端：

```dotenv
VLLM_MLLM_TOOL_TAG_FILE=deepseek-v4.1-flash.txt
```

加载方式复用[工具格式资产机制](vllm-chat-template.md#模型工具调用格式资产)。新 tool-tag 已加入 Git/Docker 白名单，随现有 Dockerfile 的 tool-tag 目录复制。Jinja 需要单独同步至模型服务器；后端镜像不负责启动 vLLM。

仅当服务端走通用 Jinja 渲染路径时，才使用 `--chat-template /绝对路径/deepseek-v4.1-flash.jinja`；标准 Transformers 环境需要提供其原生 tojson 和 raise_exception。不要在输出字符串上再次添加 BOS。

[vLLM 原生 V4.1 tokenizer](https://github.com/vllm-project/vllm/blob/main/vllm/tokenizers/deepseek_v41.py) 的 apply_chat_template 直接调用 Python encode_messages，不读取 chat_template 参数。因此原生路径不支持本模板新增的工具前后置布局，不能靠传入 Jinja 文件确认位置控制生效。要使用这些扩展，必须确认服务走自定义 Jinja 渲染路径，或另行适配原生编码器；本次没有修改外部 vLLM 编码器。应核对已安装版本的实际 tokenizer、renderer 和 V4.1 工具解析器支持；不能沿用 Qwen 的 qwen3_xml 解析器。部署参数需按目标服务版本核实，本次没有变更或重启推理服务。

---

## 验证

[离线测试](../../../tests/test_deepseek_v41_template.py) 使用官方编码器生成的 [84 组固定对照样例](../../../tests/fixtures/deepseek_v41_encoding.json)，覆盖两种模式、六种强度设置、普通多轮、system、工具定义注入、混合类型参数、多工具逆序返回、会话中 system 和多图占位。这些对照显式使用 system 布局，全部逐字一致；新增前后置、默认后置、图片顺序、连续 user 合并、固定锚点多轮前缀稳定性、首条 system 工具迁移、非法位置与索引测试。连同既有强度、调用和生成头校验，八个测试方法均通过。

```bash
python -m unittest discover -s tests -p test_deepseek_v41_template.py -v
```

测试使用与 Transformers 行为一致的 JSON filter，仅验证模板文本和边界；尚未验证真实 V4.1 模型的工具调用成功率或图像生成链路。测试目录沿用项目现有忽略规则。
