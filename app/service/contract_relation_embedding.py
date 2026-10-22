"""关系描述编码契约；只编码描述，不混入操作人、时间或合同哈希。"""

import math
from dataclasses import dataclass

from app.core.config import EmbeddingSettings
from app.core.embedding_prompt import (
    TEXT_EMBEDDING_VERSION, TEXT_EMBEDDING_INSTRUCTION, render_text_embedding_input,
)
from app.infrastructure.embedding import EmbeddingClient, EmbeddingRequestError


RELATION_EMBEDDING_VERSION = TEXT_EMBEDDING_VERSION
RELATION_EMBEDDING_INSTRUCTION = TEXT_EMBEDDING_INSTRUCTION


RELATION_QUERY_INSTRUCTION = TEXT_EMBEDDING_INSTRUCTION
RELATION_QUERY_VERSION = TEXT_EMBEDDING_VERSION


@dataclass(frozen=True, slots=True)
class RelationEmbedding:
    vector: tuple[float, ...]
    model: str
    version: str


def validate_relation_vector(vector: tuple[float, ...], dimensions: int) -> None:
    """存储前拒绝维度错误、非有限数值及零向量。"""
    if len(vector) != dimensions or not vector or any(
        isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
        for x in vector
    ):
        raise EmbeddingRequestError("关系描述向量维度或数值无效")
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or norm == 0:
        raise EmbeddingRequestError("关系描述向量范数无效")


async def embed_relation_description(description: str, settings: EmbeddingSettings) -> RelationEmbedding:
    return await _encode_relation(description, settings)


async def embed_relation_query(query: str, settings: EmbeddingSettings) -> RelationEmbedding:
    return await _encode_relation(query, settings)


async def _encode_relation(description, settings) -> RelationEmbedding:
    """一次编码并归一化；错误传播给创建接口，取消时由上下文管理器关闭连接。"""
    if not description.strip():
        raise ValueError("关系描述不能为空")
    text = render_text_embedding_input(description)
    async with EmbeddingClient(settings) as client:
        response = await client.create_embeddings(inputs=[text])
    if response.model != settings.model or len(response.vectors) != 1:
        raise EmbeddingRequestError("关系描述向量响应模型或数量不一致")
    vector = response.vectors[0]
    validate_relation_vector(vector, settings.dimensions)
    norm = math.hypot(*vector)
    return RelationEmbedding(tuple(float(x / norm) for x in vector), settings.model,
                             TEXT_EMBEDDING_VERSION)
