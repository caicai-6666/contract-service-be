"""主题规划 → Send 并发单主题生成 → 稳定顺序汇总；不递归进入 FIFO 工具管理。"""
from functools import partial
from langgraph.graph import StateGraph, START, END
from .state import SummaryInput, SummaryOutput, SummaryState, TopicMapInput
from . import node


def build_fifo_summary_subgraph(*, planner=node.run_topic_planner, generator=node.run_topic_generator):
    graph = StateGraph(SummaryState, input_schema=SummaryInput, output_schema=SummaryOutput)
    graph.add_node('plan_topics', partial(node.plan_topics, planner=planner))
    graph.add_node('generate_topic', partial(node.generate_topic, generator=generator), input_schema=TopicMapInput)
    graph.add_node('reduce_topic_summaries', node.reduce_topic_summaries)
    graph.add_edge(START, 'plan_topics')
    graph.add_conditional_edges('plan_topics', node.dispatch_topics, ['generate_topic', 'reduce_topic_summaries'])
    graph.add_edge('generate_topic', 'reduce_topic_summaries')
    graph.add_edge('reduce_topic_summaries', END)
    return graph.compile()
