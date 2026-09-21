"""合同沟通业务门禁子图装配入口。"""

from functools import partial

from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, START, StateGraph
from app.core.config import MLLMSettings

from .file_topic_conflict import (check_file_topic_conflict, check_file_topic_conflict_async,
    route_after_gate_initialization, route_after_file_topic_conflict)

from app.agent.contract_communication.business_gate.node import (
    initialize_business_gate, route_after_file_readability, route_relevance_checks,
    check_file_business_relevance, check_text_business_relevance, check_file_text_relevance, check_context_relevance,
    aggregate_relevance, summarize_files, summarize_files_async, route_after_file_summaries,
    check_text_business_relevance_async,
    check_file_business_relevance_async,
    check_file_text_relevance_async,
    check_context_relevance_async,
    reject_request, reject_request_async, route_after_relevance,
)
from app.agent.contract_communication.business_gate.state import (
    BusinessGateInput, BusinessGateSubgraphState, FileSummary, FileRelevanceInput,
    TextRelevanceInput, FileTextRelevanceInput, ContextRelevanceInput,
    FileSummaryIssue, FileSummaryFeedback, RelevanceFeedback,
)
from app.agent.contract_communication.business_gate.subgraph.file_readability import (
    build_file_readability_subgraph,
)


def build_business_gate_subgraph(*, settings: MLLMSettings | None = None,
                               max_concurrency: int = 4, max_attempts: int = 3):
    """装配可读性、并发摘要与相关性门禁；未放行统一进入拒绝回复节点。"""
    graph = StateGraph(BusinessGateSubgraphState, input_schema=BusinessGateInput)
    graph.add_node("file_readability", build_file_readability_subgraph(
        settings=settings, max_concurrency=max_concurrency, max_attempts=max_attempts))
    graph.add_node("initialize_business_gate", initialize_business_gate)
    summary_kwargs = dict(settings=settings, max_concurrency=max_concurrency, max_attempts=max_attempts)
    graph.add_node("summarize_files", RunnableLambda(
        partial(summarize_files, **summary_kwargs), afunc=partial(summarize_files_async, **summary_kwargs)))
    graph.add_node('check_file_topic_conflict', RunnableLambda(
        partial(check_file_topic_conflict, **summary_kwargs),
        afunc=partial(check_file_topic_conflict_async, **summary_kwargs)), input_schema=FileRelevanceInput)
    # 用输入 Schema 从结构上隔离原始字节、图像与视觉审计，不只依赖提示词约束。
    graph.add_node("check_file_business_relevance", RunnableLambda(
        partial(check_file_business_relevance, **summary_kwargs),
        afunc=partial(check_file_business_relevance_async, **summary_kwargs)), input_schema=FileRelevanceInput)
    text_kwargs = dict(settings=settings, max_attempts=max_attempts)
    graph.add_node("check_text_business_relevance", RunnableLambda(
        partial(check_text_business_relevance, **text_kwargs),
        afunc=partial(check_text_business_relevance_async, **text_kwargs)), input_schema=TextRelevanceInput)
    graph.add_node("check_file_text_relevance", RunnableLambda(
        partial(check_file_text_relevance, **text_kwargs),
        afunc=partial(check_file_text_relevance_async, **text_kwargs)), input_schema=FileTextRelevanceInput)
    graph.add_node("check_context_relevance", RunnableLambda(
        partial(check_context_relevance, **text_kwargs),
        afunc=partial(check_context_relevance_async, **text_kwargs)), input_schema=ContextRelevanceInput)
    graph.add_node("aggregate_relevance", aggregate_relevance)
    graph.add_node('reject_request', RunnableLambda(partial(reject_request, **text_kwargs),
                   afunc=partial(reject_request_async, **text_kwargs)))
    graph.add_edge(START, "file_readability")
    graph.add_conditional_edges('file_readability', route_after_file_readability,
                                {'end': 'reject_request', 'summarize': 'summarize_files',
                                 'continue': 'initialize_business_gate'})
    graph.add_conditional_edges('summarize_files', route_after_file_summaries,
                                {'end': 'reject_request', 'continue': 'initialize_business_gate'})
    relevance_nodes = ['check_file_business_relevance', 'check_text_business_relevance',
                       'check_file_text_relevance', 'check_context_relevance']
    graph.add_conditional_edges('initialize_business_gate', route_after_gate_initialization,
                               [*relevance_nodes, 'aggregate_relevance', 'check_file_topic_conflict'])
    graph.add_conditional_edges('check_file_topic_conflict', route_after_file_topic_conflict,
                               [*relevance_nodes, 'aggregate_relevance', 'reject_request'])
    for name in relevance_nodes:
        # 分支同属一个并行步骤；下一步仅调度一次聚合，等待该步全部分支结束。
        graph.add_edge(name, 'aggregate_relevance')
    graph.add_conditional_edges('aggregate_relevance', route_after_relevance,
                                {'pass': END, 'reject': 'reject_request'})
    graph.add_edge('reject_request', END)
    return graph.compile()


__all__ = ["BusinessGateInput", "BusinessGateSubgraphState", "FileSummary", "FileSummaryIssue", "FileSummaryFeedback", "RelevanceFeedback", "build_business_gate_subgraph"]
