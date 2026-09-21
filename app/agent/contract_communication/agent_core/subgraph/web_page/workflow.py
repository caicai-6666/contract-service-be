"""首次打开网页的串行提取图；所有业务失败均进入唯一尾部节点。"""
from functools import partial
from langgraph.graph import START, END, StateGraph
from .state import WebPageInput, WebPageOutput, WebPageState
from .node import fetch_html, extract_text, refine_content, finalize, route_after_stage


def build_web_page_subgraph(*, timeout_seconds=30, max_bytes=3 * 1024 * 1024, max_redirects=5, transport=None,
                            settings=None, client=None, audit=None, max_attempts=3):
    if timeout_seconds <= 0 or max_bytes <= 0 or max_redirects < 0:
        raise ValueError("超时与响应上限必须为正数，重定向上限不得为负数")
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("精炼尝试次数必须为正整数")
    graph = StateGraph(WebPageState, input_schema=WebPageInput, output_schema=WebPageOutput)
    graph.add_node('fetch_html', partial(fetch_html, timeout_seconds=timeout_seconds,
        max_bytes=max_bytes, max_redirects=max_redirects, transport=transport))
    graph.add_node('extract_text', extract_text)
    graph.add_node('refine_content', partial(refine_content, settings=settings, client=client,
        audit=audit, max_attempts=max_attempts))
    graph.add_node('finalize', finalize)
    graph.add_edge(START, 'fetch_html')
    graph.add_conditional_edges('fetch_html', route_after_stage,
                               {'continue': 'extract_text', 'finalize': 'finalize'})
    graph.add_conditional_edges('extract_text', route_after_stage,
                               {'continue': 'refine_content', 'finalize': 'finalize'})
    graph.add_edge('refine_content', 'finalize')
    graph.add_edge('finalize', END)
    return graph.compile()
