"""合同摘要独立编码；不编码合同名称。"""
import math

from app.core.config import EmbeddingSettings
from app.core.embedding_prompt import (
    TEXT_EMBEDDING_VERSION, TEXT_EMBEDDING_INSTRUCTION, render_text_embedding_input,
)
from app.infrastructure.contract_metadata_store import ContractTextEmbedding
from app.infrastructure.embedding import EmbeddingClient, EmbeddingRequestError

SUMMARY_EMBEDDING_VERSION = TEXT_EMBEDDING_VERSION
SUMMARY_EMBEDDING_INSTRUCTION = TEXT_EMBEDDING_INSTRUCTION


def render_contract_summary_embedding_input(summary: str) -> str:
    """只编码摘要正文，名称不作为输入，也不添加字段标签。"""
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError('合同摘要不能为空')
    return render_text_embedding_input(summary.strip())


async def embed_contract_summary(summary: str, settings: EmbeddingSettings) -> ContractTextEmbedding:
    text = render_contract_summary_embedding_input(summary)
    async with EmbeddingClient(settings) as client:
        response = await client.create_embeddings(inputs=[text])
    if response.model != settings.model or len(response.vectors) != 1:
        raise EmbeddingRequestError('合同摘要向量响应模型或数量不一致')
    vector = response.vectors[0]
    if len(vector) != settings.dimensions or any(
        isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in vector
    ):
        raise EmbeddingRequestError('合同摘要向量维度或数值无效')
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or norm == 0:
        raise EmbeddingRequestError('合同摘要向量范数无效')
    return ContractTextEmbedding(tuple(float(x/norm) for x in vector), settings.model, SUMMARY_EMBEDDING_VERSION)
