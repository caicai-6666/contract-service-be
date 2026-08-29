"""检索问题与用户查询成对 Embedding instruction。"""

from typing import Final

RETRIEVAL_EMBEDDING_PROMPT_VERSION: Final = "contract-question-retrieval-en-v1"

CONTRACT_QUESTION_EMBEDDING_INSTRUCTION: Final = (
    "Represent this contract-generated question as a retrieval facet of its source "
    "contract. Preserve the specific contractual fact being asked about and the "
    "transaction context needed to distinguish the contract."
)

USER_QUERY_EMBEDDING_INSTRUCTION: Final = (
    "Represent this user's contract question as a retrieval query for finding "
    "contracts whose generated question facets cover the same contractual fact and "
    "transaction context."
)


def render_contract_question_embedding_input(question: str) -> str:
    """按已验证的 Qwen 文本聊天边界渲染合同问题侧输入。"""
    normalized = question.strip()
    if not normalized:
        raise ValueError("待向量化的合同问题不能为空")
    return (
        f"<|im_start|>system\n{CONTRACT_QUESTION_EMBEDDING_INSTRUCTION}"
        "<|im_end|>\n"
        f"<|im_start|>user\n{normalized}<|im_end|>\n"
        "<|im_start|>assistant\n"
    )


__all__ = [
    "CONTRACT_QUESTION_EMBEDDING_INSTRUCTION",
    "RETRIEVAL_EMBEDDING_PROMPT_VERSION",
    "USER_QUERY_EMBEDDING_INSTRUCTION",
    "render_contract_question_embedding_input",
]
