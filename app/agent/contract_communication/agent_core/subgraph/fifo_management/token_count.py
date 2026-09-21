"""FIFO 按完整任务独立渲染和接口计数，统一容量记账口径。"""
import json

from app.core.config import MLLMSettings, get_settings
from app.infrastructure.vllm_tokenizer import count_text_tokens
from .schema import FIFOTask


FIFO_RENDER_VERSION = 'fifo-task-mixed-v2'


def render_fifo_task(task: FIFOTask | dict) -> str:
    """计入任务外壳及全部消息，系统提示不在计数阶段过滤。

    此文本用于任务级容量记账；不包含累计摘要、工作区、工具定义或
    聊天模板特殊 token，也不代替完整模型请求的上下文上限校验。
    """
    value = FIFOTask.model_validate(task)
    if value.rendered_content is not None:
        return value.rendered_content
    return json.dumps(value.model_dump(mode='python', exclude={'rendered_content'}), ensure_ascii=False,
                      sort_keys=True, indent=2, allow_nan=False)


async def count_fifo_task_tokens(
    tasks: list[FIFOTask], *, settings: MLLMSettings | None = None,
) -> list[int]:
    """先验证全部任务，随后顺序调用 /tokenize；失败不发布部分计数。

    各任务独立计数后求和，避免把跨任务拼接后的 BPE 差异误当作任务
    的可加计数。保持已有 counter 的逐任务列表契约，取消正常传播。
    """
    texts = [render_fifo_task(task) for task in tasks]
    if not texts:
        return []
    config = settings or get_settings().mllm
    counts = []
    for text in texts:
        counts.append(await count_text_tokens(text, config))
    return counts
