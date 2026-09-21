"""任务片段编码及查询配对指令；不实现检索工具。"""

from typing import Final, Literal

MEMORY_EMBEDDING_MODEL: Final = 'qwen3-vl-embedding-8b'
MEMORY_EMBEDDING_DIMENSIONS: Final = 4096
MEMORY_QUERY_INSTRUCTION: Final = (
    'Retrieve past interactions with a contract business assistant that are relevant '
    "to the user's current question, including prior requirements, decisions, findings, and planned actions."
)
TASK_EMBEDDING_PROMPT_VERSION: Final = 'three-view-excerpt-v2-readable-input'
TASK_DOCUMENT_INSTRUCTION: Final = (
    'Represent this excerpt of a past contract-business interaction for retrieval, '
    'preserving its topic, entities, user intent, key facts, constraints, and planned actions.'
)


EmbeddingContentKind = Literal['excerpt', 'user_question', 'file', 'final_output']
USER_QUESTION_INSTRUCTION: Final = (
    "Represent this historical user question for retrieval by a request phrased in the user's voice, "
    'focusing on the question, requested action, relevant subjects, and explicit scope and constraints. '
    'Use only information provided in the question without adding unstated information.'
)
FILE_INSTRUCTION: Final = (
    'Represent this file for retrieval using the supplied file name, display name, and summary. '
    'Match queries containing any one or more of these fields, preserving the file identity, topic, '
    'and explicitly described contents. Missing fields are allowed; do not invent or complete them.'
)
INPUT_EMBEDDING_PROMPT_VERSION: Final = 'user-input-question-file-v1'

FINAL_OUTPUT_INSTRUCTION: Final = (
    'Represent this response for retrieval, focusing on the question addressed, stated conclusions, '
    'key facts, recommendations, and unresolved matters, along with relevant subjects and applicable conditions. '
    'Preserve negation and uncertainty. Use only information explicitly provided in the text.'
)
FINAL_OUTPUT_PROMPT_VERSION: Final = 'final-response-v1'


def embedding_prompt_spec(kind: EmbeddingContentKind) -> tuple[str, str]:
    """按程序确定的内容类型选择指令，不根据正文标签猜测类型。"""
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
    instruction, _ = embedding_prompt_spec(kind)
    return (f'<|im_start|>system\n{instruction}<|im_end|>\n'
            f'<|im_start|>user\n{text}<|im_end|>\n<|im_start|>assistant\n')
