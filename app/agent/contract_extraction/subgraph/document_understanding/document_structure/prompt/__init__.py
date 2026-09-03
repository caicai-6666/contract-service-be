"""文档结构理解子图文档结构节点独占的提示词入口。"""

from app.agent.contract_extraction.subgraph.document_understanding.document_structure.prompt.unit_discovery import (
    UNIT_DISCOVERY_PROMPT_VERSION,
    build_unit_discovery_messages,
)

__all__ = [
    "UNIT_DISCOVERY_PROMPT_VERSION",
    "build_unit_discovery_messages",
]
