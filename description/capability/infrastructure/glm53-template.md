# GLM-5.3-Flash 模板

> **用途：** 为本地 GLM-5.3-Flash 提供 Jinja 消息模板、tool-call 格式提示词和统一推理强度适配。本次新增可选资产，不切换运行模型，不重启服务。

---

## 文件与官方依据

| 文件 | 职责 |
| --- | --- |
| [glm-5.3-flash.jinja](../../../data/template/glm-5.3-flash.jinja) | 保留官方消息、工具与媒体协议，增加工具注入位置及统一档位；版本 glm-5.3-flash-template-v1。 |
| [glm-5.3-flash.txt](../../../data/tool-tag/glm-5.3-flash.txt) | 工具调用格式提示词，版本 glm-5.3-tool-tag-v1；版本不进入正文。 |

依据 [官方模型卡](https://huggingface.co/zai-org/GLM-5.3-Flash) 和 [官方 Jinja](https://huggingface.co/zai-org/GLM-5.3-Flash/blob/main/chat_template.jinja)。2026-09-20 下载的官方模板 SHA-256：`0c4099f3382d6c92700dfb99725025360966fd73032f0ecf32377c0d9e6309c5`。官方 MIT 许可保存在项目模板末尾注释中，不额外创建许可文件，也不进入模型输入。

---

## 工具与消息协议

保留 `[gMASK]<sop>`、system/user/assistant/observation 角色标记，以及 think 和 tool_response 格式。工具调用采用 GLM 格式：

```text
<tool_call>lookup<arg_key>query</arg_key><arg_value>合同名称</arg_value><arg_key>page</arg_key><arg_value>1</arg_value></tool_call>
```

字符串为原文，其他值为 JSON，包括 null、布尔值、数组及嵌套对象。历史 tool_calls 的 arguments 必须先解析为字典；传入 JSON 字符串会明确报错。工具定义通过 tools 注入，保留官方去除函数级 strict/defer_loading 元数据、延迟工具引用及连续工具结果排序逻辑。排序条件不成立时，按官方行为保留返回顺序。单轮调用数量继续由节点规则与程序校验决定。

模板保留官方图片、视频、音频占位符渲染路径；占位符不代表服务具备对应媒体处理能力。本项目没有完成 GLM 的实际图片、视频或音频推理验证，媒体加载及视觉 token 展开依赖服务端。未知消息角色、未知内容块明确拒绝，避免资料静默丢失。

---

## 工具注入位置

| 参数 | 说明 |
| --- | --- |
| tool_placement=after_task | 默认；公共消息 → 初始任务（包括媒体）→ 工具定义 → 后续轨迹。 |
| tool_placement=before_task | 公共消息 → 工具定义 → 初始任务 → 后续轨迹。 |
| tool_placement=system | 保留官方开头 system 工具块，供兼容与对照。 |
| tool_task_index | 原始 messages 中 user 的零基索引，固定初始任务；不允许布尔值、越界值或指向非 user。 |

未指定锚点时，使用最后一个真实 user，排除整段 tool_response 包装的反馈文本。新增纠错 user 会影响默认定位，多轮节点应显式指定初始任务。锚点与 clear_thinking 的历史裁剪判断独立。工具定义仅注入一次，无 tools 时不创建工具块。

前后置在锚点 user 内容内部注入工具，是项目扩展，不是官方布局；离线位置正确不代表模型工具成功率已经验证。应用现有客户端支持两种任务位置，system 模式可通过模板直接调用。只接受请求级 tools，不将消息上的私有 tools 字段作为替代输入。

---

## 推理强度与思考边界

| 统一环境配置 | GLM 原生值 |
| --- | --- |
| low | low |
| medium | high |
| xhigh（默认） | max |

选用 `VLLM_MLLM_TOOL_TAG_FILE=glm-5.3-flash.txt` 后，MLLMSettings.thinking_template_kwargs 自动转换请求值；生成与聊天 token 计数共用此入口。Jinja 也直接接受统一三档，并兼容原生 high/max。非法值明确拒绝，不沿用官方对未知值自动回退 max 的行为。

**思考开关边界：** 官方模板始终追加 `<think>`，没有 enable_thinking=false 分支。项目模板不虚构关闭能力，显式传入 false 时会报错。迁移时不仅要打开 Agent Core 的全局开关，还必须检查业务门禁及其他专用节点是否显式传入 false；仅切换环境变量不能保证整个系统已经兼容 GLM。当前适配仅统一强度，不改写各节点开关。

clear_thinking 默认 false，保留请求已提供的历史思考；true 按官方规则清除最后一条 user 之前的历史思考。它控制历史保留，不是关闭本轮思考。上游思考 FIFO 仍决定实际传入哪些条目，模板不会恢复被上游删除的内容。

---

## 使用与部署

切换时使用以下配置示例，模型名必须匹配服务端别名：

```dotenv
VLLM_MLLM_MODEL=glm-5.3-flash
VLLM_MLLM_TOOL_TAG_FILE=glm-5.3-flash.txt
VLLM_MLLM_ENABLE_THINKING=true
VLLM_MLLM_REASONING_EFFORT=medium
```

tool-tag 复用[启动加载机制](vllm-chat-template.md#模型工具调用格式资产)，已加入 Git/Docker 白名单并随后端镜像复制；Jinja 单独同步到推理服务器。对支持该模型的 vLLM，模型相关启动选项参考：

```text
--enable-auto-tool-choice
--tool-call-parser glm47
--reasoning-parser glm47
--chat-template /绝对路径/glm-5.3-flash.jinja
```

以上只列协议参数，不是完整部署命令。解析器名称依据当前 [vLLM 官方配方](https://recipes.vllm.ai/zai-org/GLM-5.3-Flash)，应与已安装版本核对，不能继续使用 Qwen/DeepSeek 解析器。Jinja 环境需要 Transformers 提供的 tojson、raise_exception 和 loopcontrols 扩展。实际切换需重启后端与加载模板的推理服务。

---

## 验证

[模板测试](../../../tests/test_glm53_template.py)与[30 组官方对照样例](../../../tests/fixtures/glm53_encoding.json)覆盖三档强度、历史思考保留/清理、普通消息、工具参数和逆序结果、媒体占位。system 模式逐字一致；项目扩展测试覆盖统一档位、前后位置、固定锚点多轮前缀稳定性、非法参数、生成头与实际 tool-tag 加载。

统一请求测试同时覆盖 GLM 的三类生成请求及分词映射。所有验证为离线验证，不请求实际模型；尚未验证真实模型工具调用质量、流式解析和强制 JSON 生成。测试目录沿用项目现有 Git 忽略规则。

```bash
python -m unittest discover -s tests -p test_glm53_template.py -v
python -m unittest discover -s tests -p test_mllm_reasoning_effort.py -v
```
