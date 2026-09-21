"""工作区管理子图装配；只绑定依赖与路由，不创建 tokenizer 或模型客户端。"""
from functools import partial

from langgraph.graph import END, START, StateGraph
from .node import (
    estimate_modified_tokens, choose_capacity_branch, route_capacity,
    compress_before_modification, apply_after_compression, apply_without_compression,
    build_final_result,
)
from .state import WorkspaceManagementInput, WorkspaceManagementOutput, WorkspaceManagementState
from .token_count import count_workspace_tokens


def build_workspace_management_subgraph(*, token_budget: int, counter=count_workspace_tokens, compressor=None):
    """默认通过 vLLM 接口计数；满容量默认运行压缩子 Agent，允许注入自定义压缩实现。"""
    if type(token_budget) is not int or token_budget <= 0:
        raise ValueError('token_budget 必须为正整数')
    graph = StateGraph(WorkspaceManagementState, input_schema=WorkspaceManagementInput,
                       output_schema=WorkspaceManagementOutput)
    graph.add_node('estimate_modified_tokens', partial(estimate_modified_tokens, counter=counter))
    graph.add_node('choose_capacity_branch', partial(choose_capacity_branch, token_budget=token_budget))
    graph.add_node('compress_before_modification', partial(compress_before_modification,
                   token_budget=token_budget, compressor=compressor, counter=counter))
    graph.add_node('apply_after_compression', partial(apply_after_compression, token_budget=token_budget, counter=counter))
    graph.add_node('apply_without_compression', apply_without_compression)
    graph.add_node('build_final_result', partial(build_final_result, token_budget=token_budget))
    graph.add_edge(START, 'estimate_modified_tokens')
    graph.add_edge('estimate_modified_tokens', 'choose_capacity_branch')
    graph.add_conditional_edges('choose_capacity_branch', route_capacity, {
        'error': 'build_final_result', 'full': 'compress_before_modification',
        'warning': 'apply_without_compression', 'normal': 'apply_without_compression',
    })
    graph.add_edge('compress_before_modification', 'apply_after_compression')
    for node in ('apply_after_compression', 'apply_without_compression'):
        graph.add_edge(node, 'build_final_result')
    graph.add_edge('build_final_result', END)
    return graph.compile()
