"""核心字段提取子图的提示词入口。"""

from app.agent.contract_extraction.subgraph.field_extraction.core_field.prompt.extraction import (
    CORE_FIELD_EXTRACTION_PROMPT_VERSION,
    FIELD_DEFINITION_GUIDE,
    build_core_field_messages,
)

__all__ = [
    "CORE_FIELD_EXTRACTION_PROMPT_VERSION",
    "FIELD_DEFINITION_GUIDE",
    "build_core_field_messages",
]
