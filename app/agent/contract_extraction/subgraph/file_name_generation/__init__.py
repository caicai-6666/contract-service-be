"""合同建议文件名生成子图装配入口。"""

from langgraph.graph import END, START, StateGraph

from app.agent.contract_extraction.subgraph.file_name_generation.node import (
    assemble_file_name_context,
    generate_suggested_file_name,
)
from app.agent.contract_extraction.subgraph.file_name_generation.state import (
    FileNameGenerationSubgraphState,
)
from app.agent.contract_extraction.subgraph.file_name_generation.tool import (
    FILE_NAME_GENERATION_TOOLS,
    FILE_NAME_GENERATION_TOOL_CHOICE,
    FILE_NAME_GENERATION_TOOL_PLACEMENT,
    FILE_NAME_GENERATION_TOOL_VERSION,
    parse_file_name_generation_tool_arguments,
    validation_error_feedback,
)


def build_file_name_generation_subgraph():
    """装配“上下文组装 → 建议文件名生成”的两节点子图骨架。"""
    graph = StateGraph(FileNameGenerationSubgraphState)
    graph.add_node("assemble_file_name_context", assemble_file_name_context)
    graph.add_node("generate_suggested_file_name", generate_suggested_file_name)
    graph.add_edge(START, "assemble_file_name_context")
    graph.add_edge("assemble_file_name_context", "generate_suggested_file_name")
    graph.add_edge("generate_suggested_file_name", END)
    return graph.compile()


__all__ = [
    "FILE_NAME_GENERATION_TOOLS",
    "FILE_NAME_GENERATION_TOOL_CHOICE",
    "FILE_NAME_GENERATION_TOOL_PLACEMENT",
    "FILE_NAME_GENERATION_TOOL_VERSION",
    "build_file_name_generation_subgraph",
    "parse_file_name_generation_tool_arguments",
    "validation_error_feedback",
]
