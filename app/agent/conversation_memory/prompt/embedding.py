"""任务编码类型与审计入口；实际文本指令统一使用通用检索契约。"""

from typing import Final, Literal
from app.core.embedding_prompt import (
    TEXT_EMBEDDING_VERSION, TEXT_EMBEDDING_INSTRUCTION, render_text_embedding_input,
)

MEMORY_EMBEDDING_MODEL: Final = 'qwen3-vl-embedding-8b'
MEMORY_EMBEDDING_DIMENSIONS: Final = 4096
MEMORY_QUERY_INSTRUCTION: Final = TEXT_EMBEDDING_INSTRUCTION
TASK_EMBEDDING_PROMPT_VERSION: Final = TEXT_EMBEDDING_VERSION
TASK_DOCUMENT_INSTRUCTION: Final = TEXT_EMBEDDING_INSTRUCTION


EmbeddingContentKind = Literal['excerpt', 'user_question', 'file', 'final_output']
USER_QUESTION_INSTRUCTION: Final = TEXT_EMBEDDING_INSTRUCTION
FILE_INSTRUCTION: Final = TEXT_EMBEDDING_INSTRUCTION
INPUT_EMBEDDING_PROMPT_VERSION: Final = TEXT_EMBEDDING_VERSION

FINAL_OUTPUT_INSTRUCTION: Final = TEXT_EMBEDDING_INSTRUCTION
FINAL_OUTPUT_PROMPT_VERSION: Final = TEXT_EMBEDDING_VERSION


def embedding_prompt_spec(kind: EmbeddingContentKind) -> tuple[str, str]:
    """保留类型校验及分项审计；所有文本类型采用同一编码版本。"""
    if kind == 'excerpt':
        return TASK_DOCUMENT_INSTRUCTION, TASK_EMBEDDING_PROMPT_VERSION
    if kind == 'user_question':
        return USER_QUESTION_INSTRUCTION, INPUT_EMBEDDING_PROMPT_VERSION
    if kind == 'file':
        return FILE_INSTRUCTION, INPUT_EMBEDDING_PROMPT_VERSION
    if kind == 'final_output':
        return FINAL_OUTPUT_INSTRUCTION, FINAL_OUTPUT_PROMPT_VERSION
    raise ValueError('未知的记忆编码内容类型')


def render_task_embedding_input(text: str, *, kind: EmbeddingContentKind = 'excerpt') -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError('待编码的任务片段不能为空')
    embedding_prompt_spec(kind)
    return render_text_embedding_input(text)
