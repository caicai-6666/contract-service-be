"""任务片段向量化及校验；不访问SQLite。"""

import math
from time import perf_counter

from app.agent.conversation_memory.prompt.embedding import (
    MEMORY_EMBEDDING_MODEL, MEMORY_EMBEDDING_DIMENSIONS,
    render_task_embedding_input, embedding_prompt_spec, EmbeddingContentKind,
)
from app.core.config import get_settings
from app.infrastructure.embedding import EmbeddingClient


async def embed_task_text(text: str, audit: dict, *, kind: EmbeddingContentKind = 'excerpt') -> tuple[float, ...]:
    """调用一次Embedding；失败只记录类型，取消继续传播并关闭连接。"""
    tick = perf_counter()
    audit.update(content_kind=kind, accepted=False)
    try:
        _, version = embedding_prompt_spec(kind)
        audit['prompt_version'] = version
        settings = get_settings().embedding
        if settings.model != MEMORY_EMBEDDING_MODEL or settings.dimensions != MEMORY_EMBEDDING_DIMENSIONS:
            raise ValueError('模型或维度不符合已确认的会话记忆编码方案')
        async with EmbeddingClient(settings) as client:
            response = await client.create_embeddings(inputs=[render_task_embedding_input(text, kind=kind)])
        audit.update(model=response.model, prompt_tokens=response.prompt_tokens,
                     vector_count=len(response.vectors))
        if response.model != settings.model or len(response.vectors) != 1:
            raise ValueError('Embedding响应模型或数量不一致')
        vector = response.vectors[0]
        if len(vector) != MEMORY_EMBEDDING_DIMENSIONS or any(
            isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in vector
        ):
            raise ValueError('Embedding向量维度或数值无效')
        norm = math.hypot(*vector)
        if not math.isfinite(norm) or norm == 0:
            raise ValueError('Embedding向量范数无效')
        normalized = tuple(float(x / norm) for x in vector)
        audit.update(accepted=True, dimensions=len(vector), input_norm=norm)
        return normalized
    except Exception as exc:
        audit.update(error_type=type(exc).__name__)
        raise
    finally:
        audit['elapsed_ms'] = (perf_counter() - tick) * 1000
