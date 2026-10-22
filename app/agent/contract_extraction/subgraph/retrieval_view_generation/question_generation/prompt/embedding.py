"""检索问题与查询共用领域无关的文本向量指令。"""

from typing import Final
from app.core.embedding_prompt import (
    TEXT_EMBEDDING_VERSION, TEXT_EMBEDDING_INSTRUCTION, render_text_embedding_input,
)

RETRIEVAL_EMBEDDING_PROMPT_VERSION: Final = TEXT_EMBEDDING_VERSION

CONTRACT_QUESTION_EMBEDDING_INSTRUCTION: Final = TEXT_EMBEDDING_INSTRUCTION

USER_QUERY_EMBEDDING_INSTRUCTION: Final = TEXT_EMBEDDING_INSTRUCTION


def render_contract_question_embedding_input(question: str) -> str:
    """按已验证的 Qwen 文本聊天边界渲染合同问题侧输入。"""
    normalized = question.strip()
    if not normalized:
        raise ValueError("待向量化的合同问题不能为空")
    return render_text_embedding_input(normalized)


def render_user_query_embedding_input(query: str) -> str:
    """使用与入库问题配对的查询侧指令，不补造答案或新的检索问题。"""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("合同检索问题不能为空")
    return render_text_embedding_input(query.strip())


__all__ = [
    "render_user_query_embedding_input",
    "CONTRACT_QUESTION_EMBEDDING_INSTRUCTION",
    "RETRIEVAL_EMBEDDING_PROMPT_VERSION",
    "USER_QUERY_EMBEDDING_INSTRUCTION",
    "render_contract_question_embedding_input",
]
