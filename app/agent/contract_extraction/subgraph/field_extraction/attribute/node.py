"""Attribute 提取子图的节点占位。"""

from app.agent.contract_extraction.state import WorkflowPlaceholder
from app.agent.contract_extraction.subgraph.field_extraction.attribute.state import (
    AttributeSubgraphState,
)


def extract_attribute_placeholder(
    state: AttributeSubgraphState,
) -> AttributeSubgraphState:
    """预留 Attribute 提取能力。"""
    return {
        "attribute": WorkflowPlaceholder(
            node="extract_attribute",
            message="待接入 Attribute Profile、Core 上下文和 Schema 校验。",
        )
    }
