"""合同信息抽取子图的公开装配入口。"""

from typing import Any


def build_clause_extraction_subgraph() -> Any:
    """延迟导入并装配条款提取子图，避免包入口形成循环依赖。"""
    from app.agent.contract_extraction.subgraph.clause_extraction import (
        build_clause_extraction_subgraph as build,
    )

    return build()


def build_classification_subgraph() -> Any:
    """延迟导入并装配合同分类子图。"""
    from app.agent.contract_extraction.subgraph.classification import (
        build_classification_subgraph as build,
    )

    return build()


def build_field_extraction_subgraph() -> Any:
    """延迟导入并装配字段提取子图。"""
    from app.agent.contract_extraction.subgraph.field_extraction import (
        build_field_extraction_subgraph as build,
    )

    return build()


def build_preheat_subgraph() -> Any:
    """延迟导入并装配最终预热子图。"""
    from app.agent.contract_extraction.subgraph.preheat import (
        build_preheat_subgraph as build,
    )

    return build()


def build_preprocessing_subgraph() -> Any:
    """延迟导入并装配 PDF 预处理子图。"""
    from app.agent.contract_extraction.subgraph.preprocessing import (
        build_preprocessing_subgraph as build,
    )

    return build()


def build_summary_generation_subgraph() -> Any:
    """延迟导入并装配摘要生成子图。"""
    from app.agent.contract_extraction.subgraph.summary_generation import (
        build_summary_generation_subgraph as build,
    )

    return build()

__all__ = [
    "build_clause_extraction_subgraph",
    "build_classification_subgraph",
    "build_field_extraction_subgraph",
    "build_preheat_subgraph",
    "build_preprocessing_subgraph",
    "build_summary_generation_subgraph",
]
