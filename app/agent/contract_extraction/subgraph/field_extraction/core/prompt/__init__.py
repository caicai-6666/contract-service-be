"""核心字段提取子图的提示词入口。"""

from app.agent.contract_extraction.subgraph.field_extraction.core.prompt.extraction import (
    CORE_COMMON_PROMPT_VERSION,
    CORE_EXTRACTION_PROMPT_VERSION,
    FIELD_DEFINITION_GUIDE,
    append_core_prefill_task,
    build_core_common_messages,
    build_core_messages,
)

__all__ = [
    "CORE_COMMON_PROMPT_VERSION",
    "CORE_EXTRACTION_PROMPT_VERSION",
    "FIELD_DEFINITION_GUIDE",
    "append_core_prefill_task",
    "build_core_common_messages",
    "build_core_messages",
]
