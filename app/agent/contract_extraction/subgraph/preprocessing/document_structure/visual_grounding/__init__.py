"""文档单元视觉定位节点入口。"""

from app.agent.contract_extraction.subgraph.preprocessing.document_structure.visual_grounding.node import (
    locate_document_units,
)

# 工具保持为包内实现细节，不从预处理子图的公共入口导出。
__all__ = ["locate_document_units"]
