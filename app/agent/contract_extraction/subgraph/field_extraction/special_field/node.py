"""特殊字段提取子图的节点占位。"""

from app.agent.contract_extraction.state import WorkflowPlaceholder
from app.agent.contract_extraction.subgraph.field_extraction.special_field.state import (
    SpecialFieldSubgraphState,
)


def extract_special_field_placeholder(
    state: SpecialFieldSubgraphState,
) -> SpecialFieldSubgraphState:
    """预留特殊字段提取能力。"""
    return {
        "special_field": WorkflowPlaceholder(
            node="extract_special_field",
            message="待接入特殊字段目录、核心字段上下文和 Schema 校验。",
        )
    }
