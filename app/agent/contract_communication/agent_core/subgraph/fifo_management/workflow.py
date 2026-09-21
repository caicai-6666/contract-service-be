"""FIFO 子图拓扑；只供主助手调用，不自动注册工具或创建模型客户端。"""
from functools import partial
from langgraph.graph import START, END, StateGraph
from . import node
from .compression import summarize_fifo_placeholder, count_fifo_summary_tokens
from .token_count import count_fifo_task_tokens
from .state import FIFOManagementInput, FIFOManagementOutput, FIFOManagementState


def build_fifo_management_subgraph(*, token_budget: int, counter=count_fifo_task_tokens, executor=None,
                                   compressor=summarize_fifo_placeholder, summary_counter=count_fifo_summary_tokens, summary_commit=None, supports_foldable=False):
    """默认通过 vLLM 接口逐任务计数；可注入异步 counter，装配阶段不发请求。"""
    if type(token_budget) is not int or token_budget <= 0:
        raise ValueError('token_budget 必须为正整数')
    graph = StateGraph(FIFOManagementState, input_schema=FIFOManagementInput,
                       output_schema=FIFOManagementOutput)
    graph.add_node('count_fifo_tokens', partial(node.count_fifo_tokens, counter=counter))
    graph.add_node('choose_capacity_branch', partial(node.choose_capacity_branch, token_budget=token_budget))
    graph.add_node('execute_after_compression', partial(node.execute_after_compression, executor=executor, supports_foldable=supports_foldable))
    graph.add_node('execute_without_compression', partial(node.execute_without_compression, executor=executor, supports_foldable=supports_foldable))
    graph.add_node('recheck_after_execution', partial(node.recheck_after_execution, token_budget=token_budget, counter=counter))
    for name in ('compress_before_execution', 'compress_after_execution'):
        graph.add_node(name, partial(getattr(node, name), token_budget=token_budget, counter=counter,
                                     compressor=compressor, summary_counter=summary_counter, summary_commit=summary_commit))
    graph.add_node('build_final_result', node.build_final_result)
    graph.add_edge(START, 'count_fifo_tokens')
    graph.add_edge('count_fifo_tokens', 'choose_capacity_branch')
    graph.add_conditional_edges('choose_capacity_branch', lambda state: state['branch'], {
        'error': 'build_final_result', 'compress': 'compress_before_execution',
        'execute': 'execute_without_compression',
    })
    graph.add_edge('compress_before_execution', 'execute_after_compression')
    graph.add_edge('execute_after_compression', 'recheck_after_execution')
    graph.add_edge('execute_without_compression', 'recheck_after_execution')
    graph.add_conditional_edges('recheck_after_execution', node.route_after_execution, {
        'compress': 'compress_after_execution', 'final': 'build_final_result',
    })
    graph.add_edge('compress_after_execution', 'build_final_result')
    graph.add_edge('build_final_result', END)
    return graph.compile()
