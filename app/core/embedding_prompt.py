"""领域无关的向量编码契约；存储侧和查询侧共享同一指令及版本。"""

from typing import Final, Literal


TEXT_EMBEDDING_VERSION: Final = 'generic-text-retrieval-en-v1'
TEXT_EMBEDDING_INSTRUCTION: Final = (
    'Represent this text for semantic retrieval. Preserve its topic, entities, relationships, '
    'actions, conditions, negation and uncertainty. Use only information explicitly present.'
)

ImageEmbeddingVersion = Literal['generic-image-retrieval-zh-v1']
IMAGE_EMBEDDING_VERSION: Final[ImageEmbeddingVersion] = 'generic-image-retrieval-zh-v1'
IMAGE_EMBEDDING_INSTRUCTION: Final = (
    '为检索表示这张图像，保留可见内容、文字、名称、数字、空间关系和版式。'
    '忽略轻微缩放、压缩及扫描噪声，保留实质内容差异。'
)


def render_text_embedding_input(text: str) -> str:
    """保留正文原有标签和空白，只拒绝空输入；业务格式化仍由调用方负责。"""
    if not isinstance(text, str) or not text.strip():
        raise ValueError('待向量化的文本不能为空')
    return (f'<|im_start|>system\n{TEXT_EMBEDDING_INSTRUCTION}<|im_end|>\n'
            f'<|im_start|>user\n{text}<|im_end|>\n<|im_start|>assistant\n')
