"""注意事项新增：在 SQLite 事务外编码，写入时重新检查合同状态。"""

import asyncio
import math

from app.core.config import EmbeddingSettings
from app.infrastructure.contract_metadata_store import (
    SQLiteContractMetadataStore, ContractTextEmbedding, ContractMetadataStatus,
    ContractMetadataNotFoundError, ContractMetadataStateError,
)
from app.infrastructure.embedding import EmbeddingClient, EmbeddingRequestError

NOTE_EMBEDDING_VERSION = 'contract-note-v1'
NOTE_EMBEDDING_INSTRUCTION = (
    'Represent this user-authored note about a contract for retrieval. Preserve its topic, '
    'observations, concerns, reminders, conditions, negation and uncertainty. '
    'Treat the content as a user note, not as verified contract terms. Do not add unstated facts.'
)


async def embed_contract_note(content: str, settings: EmbeddingSettings) -> ContractTextEmbedding:
    """每条正文单独编码，不混入作者、时间、合同 ID 或合同摘要。"""
    if not isinstance(content, str) or not content.strip() or len(content.strip()) > 10000:
        raise ValueError('注意事项须为1至10000字符')
    text = (f'<|im_start|>system\n{NOTE_EMBEDDING_INSTRUCTION}<|im_end|>\n'
            f'<|im_start|>user\n{content.strip()}<|im_end|>\n<|im_start|>assistant\n')
    async with EmbeddingClient(settings) as client:
        response = await client.create_embeddings(inputs=[text])
    if response.model != settings.model or len(response.vectors) != 1:
        raise EmbeddingRequestError('注意事项向量响应模型或数量不一致')
    vector = response.vectors[0]
    if len(vector) != settings.dimensions or any(
        isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in vector
    ):
        raise EmbeddingRequestError('注意事项向量维度或数值无效')
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or norm == 0:
        raise EmbeddingRequestError('注意事项向量范数无效')
    return ContractTextEmbedding(tuple(float(x / norm) for x in vector), settings.model, NOTE_EMBEDDING_VERSION)


async def create_contract_note(store: SQLiteContractMetadataStore, document_id: str, *,
                               content: str, author_name: str, settings: EmbeddingSettings) -> dict[str, str]:
    metadata = await asyncio.to_thread(store.get, document_id)
    if metadata is None:
        raise ContractMetadataNotFoundError('合同不存在或已删除')
    if metadata.status is not ContractMetadataStatus.READY:
        raise ContractMetadataStateError('合同尚未完成入库或正在删除')
    if not author_name.strip():
        raise ValueError('撰写人不能为空')
    embedding = await embed_contract_note(content, settings)
    # 编码期间合同可能被删除或进入删除状态，存储层在写事务内再次校验。
    return await asyncio.to_thread(store.add_note, document_id, content=content,
                                   author_name=author_name, embedding=embedding)
