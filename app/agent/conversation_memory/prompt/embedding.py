"""会话历史检索的固定配对指令；仅实现总结侧输入，不实现查询工具。"""

from typing import Final

MEMORY_EMBEDDING_PROMPT_VERSION: Final = 'contract-history-pair-v1'
MEMORY_EMBEDDING_MODEL: Final = 'qwen3-vl-embedding-8b'
MEMORY_EMBEDDING_DIMENSIONS: Final = 4096
MEMORY_QUERY_INSTRUCTION: Final = (
    'Retrieve past interactions with a contract business assistant that are relevant '
    "to the user's current question, including prior requirements, decisions, findings, and planned actions."
)
MEMORY_DOCUMENT_INSTRUCTION: Final = (
    'Represent this summary of a past contract-business interaction for retrieval, '
    'preserving its topic, entities, user intent, key facts, constraints, and planned actions.'
)


def render_memory_embedding_input(retrieval_text: str) -> str:
    """只编码正式正文，不添加任务ID、时间元数据、模型推理或审计。"""
    if not isinstance(retrieval_text, str) or not retrieval_text.strip():
        raise ValueError('待向量化的记忆正文不能为空')
    return (f'<|im_start|>system\n{MEMORY_DOCUMENT_INSTRUCTION}<|im_end|>\n'
            f'<|im_start|>user\n{retrieval_text}<|im_end|>\n<|im_start|>assistant\n')
