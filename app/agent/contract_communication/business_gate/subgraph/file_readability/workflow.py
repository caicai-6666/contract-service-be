"""文件可读性子图装配：绑定节点依赖、连接 Map-Reduce 分支与设置并发上限。"""

from functools import partial

from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, START, StateGraph

from app.infrastructure.mllm import MLLMClient
from .node import (
    open_files, dispatch_files, render_file, finish_file, collect_file_results,
    check_file_visual_readability, check_file_visual_readability_sync, route_after_render_check,
)
from .state import (
    FileReadabilityState, ReadabilityInput, ReadabilityGraphState, FileBranchState, FileBranchOutput,
)


def build_readability_workflow(*, settings=None, max_concurrency=4, max_attempts=3, client_factory=MLLMClient):
    if type(max_concurrency) is not int or max_concurrency < 1:
        raise ValueError('max_concurrency 必须为正整数')
    if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
        raise ValueError('max_attempts 必须为 1 至 3 的整数')

    # 装配只绑定参数，不在 workflow 中定义节点逻辑。
    visual_kwargs = dict(settings=settings, max_attempts=max_attempts, client_factory=client_factory)
    visual_runnable = RunnableLambda(
        partial(check_file_visual_readability_sync, **visual_kwargs),
        afunc=partial(check_file_visual_readability, **visual_kwargs),
    )
    branch = StateGraph(FileBranchState, output_schema=FileBranchOutput)
    branch.add_node('check_file_renderable', partial(render_file, settings=settings))
    branch.add_node('check_file_visual_readability', visual_runnable)
    branch.add_node('finish_file', finish_file)
    branch.add_edge(START, 'check_file_renderable')
    branch.add_conditional_edges('check_file_renderable',
        route_after_render_check,
        {'visual': 'check_file_visual_readability', 'finish': 'finish_file'})
    branch.add_edge('check_file_visual_readability', 'finish_file')
    branch.add_edge('finish_file', END)

    graph = StateGraph(ReadabilityGraphState, input_schema=ReadabilityInput, output_schema=FileReadabilityState)
    graph.add_node('check_files_openable', open_files)
    graph.add_node('check_file', branch.compile())
    graph.add_node('collect_file_results', collect_file_results)
    graph.add_edge(START, 'check_files_openable')
    graph.add_conditional_edges('check_files_openable', dispatch_files, ['check_file', END])
    graph.add_edge('check_file', 'collect_file_results')
    graph.add_edge('collect_file_results', END)
    return graph.compile().with_config({'max_concurrency': max_concurrency})
