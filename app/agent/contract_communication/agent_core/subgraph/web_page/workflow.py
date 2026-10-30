"""首次打开网页的串行提取图；所有业务失败均进入唯一尾部节点。"""
from functools import partial
from langgraph.graph import START, END, StateGraph
from .state import WebPageInput, WebPageOutput, WebPageState
from .node import fetch_html, extract_text, load_images, refine_content, finalize, route_after_stage


def build_web_page_subgraph(*, timeout_seconds=30, max_bytes=3 * 1024 * 1024, max_redirects=5, transport=None,
                            settings=None, client=None, audit=None, max_attempts=3,
                            image_max_candidates=6, image_max_selected=3, image_max_bytes=4*1024*1024,
                            image_transport=None, image_resolver=None):
    if timeout_seconds <= 0 or max_bytes <= 0 or max_redirects < 0:
        raise ValueError("超时与响应上限必须为正数，重定向上限不得为负数")
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("精炼尝试次数必须为正整数")
    if any(type(n) is not int or n < 1 for n in (image_max_candidates, image_max_selected, image_max_bytes)):
        raise ValueError('图片数量和字节上限必须为正整数')
    graph = StateGraph(WebPageState, input_schema=WebPageInput, output_schema=WebPageOutput)
    graph.add_node('fetch_html', partial(fetch_html, timeout_seconds=timeout_seconds,
        max_bytes=max_bytes, max_redirects=max_redirects, transport=transport))
    graph.add_node('extract_text', partial(extract_text, max_images=image_max_candidates))
    graph.add_node('load_images', partial(load_images, transport=image_transport,
        max_bytes=image_max_bytes, resolver=image_resolver))
    graph.add_node('refine_content', partial(refine_content, settings=settings, client=client,
        audit=audit, max_attempts=max_attempts, max_images=image_max_selected))
    graph.add_node('finalize', finalize)
    graph.add_edge(START, 'fetch_html')
    graph.add_conditional_edges('fetch_html', route_after_stage,
                               {'continue': 'extract_text', 'finalize': 'finalize'})
    graph.add_conditional_edges('extract_text', route_after_stage,
                               {'continue': 'load_images', 'finalize': 'finalize'})
    graph.add_edge('load_images', 'refine_content')
    graph.add_edge('refine_content', 'finalize')
    graph.add_edge('finalize', END)
    return graph.compile()
