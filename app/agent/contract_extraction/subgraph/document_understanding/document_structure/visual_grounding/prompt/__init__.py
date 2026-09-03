"""单元视觉定位节点的私有提示词入口。"""

from app.agent.contract_extraction.subgraph.document_understanding.document_structure.visual_grounding.prompt.grounding import (
    UNIT_VISUAL_GROUNDING_COMMON_TASK,
    UNIT_VISUAL_GROUNDING_PROMPT_VERSION,
    build_unit_visual_grounding_messages,
)

__all__ = [
    "UNIT_VISUAL_GROUNDING_COMMON_TASK",
    "UNIT_VISUAL_GROUNDING_PROMPT_VERSION",
    "build_unit_visual_grounding_messages",
]
