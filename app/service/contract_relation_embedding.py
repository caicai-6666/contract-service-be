"""关系描述编码契约；只编码描述，不混入操作人、时间或合同哈希。"""

import math
from dataclasses import dataclass

from app.core.config import EmbeddingSettings
from app.infrastructure.embedding import EmbeddingClient, EmbeddingRequestError


RELATION_EMBEDDING_VERSION = "contract-relation-description-v1"
RELATION_EMBEDDING_INSTRUCTION = (
    "Represent this description of a relationship between contracts for retrieval, "
    "preserving the stated roles of the contracts, the reason for their association, "
    "and explicit conditions, negation and uncertainty. Do not add unstated facts."
)


RELATION_QUERY_INSTRUCTION = (
    'Represent this description of a desired relationship between contracts '
    'to retrieve matching user-authored relationship descriptions. '
    'Focus on the relationship type, the stated roles of the contracts, '
    'the reason for their association, and explicit conditions. '
    'Preserve negation and uncertainty. Use only the supplied information; '
    'do not infer missing roles or facts.'
)
RELATION_QUERY_VERSION = 'contract-relation-query-v1'


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
    return await _encode_relation(description, settings, RELATION_EMBEDDING_INSTRUCTION, RELATION_EMBEDDING_VERSION)


async def embed_relation_query(query: str, settings: EmbeddingSettings) -> RelationEmbedding:
    return await _encode_relation(query, settings, RELATION_QUERY_INSTRUCTION, RELATION_QUERY_VERSION)


async def _encode_relation(description, settings, instruction, version) -> RelationEmbedding:
    """一次编码并归一化；错误传播给创建接口，取消时由上下文管理器关闭连接。"""
    if not description.strip():
        raise ValueError("关系描述不能为空")
    text = (f"<|im_start|>system\n{instruction}<|im_end|>\n"
            f"<|im_start|>user\n{description}<|im_end|>\n<|im_start|>assistant\n")
    async with EmbeddingClient(settings) as client:
        response = await client.create_embeddings(inputs=[text])
    if response.model != settings.model or len(response.vectors) != 1:
        raise EmbeddingRequestError("关系描述向量响应模型或数量不一致")
    vector = response.vectors[0]
    validate_relation_vector(vector, settings.dimensions)
    norm = math.hypot(*vector)
    return RelationEmbedding(tuple(float(x / norm) for x in vector), settings.model,
                             version)
