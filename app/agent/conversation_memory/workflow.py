"""单任务建模→按实际内容三路编码→收束；不筛选任务、不写SQLite。"""
from langgraph.graph import END, START, StateGraph
from app.agent.conversation_memory.node import (
    TaskMemoryState, TaskMemoryInputState, TaskMemoryOutputState,
    model_task, active_branches, embed_user_input, embed_intermediate_outputs,
    embed_final_output, collect_task_retrieval,
)


def build_conversation_memory_graph():
    graph = StateGraph(TaskMemoryState, input_schema=TaskMemoryInputState, output_schema=TaskMemoryOutputState)
    graph.add_node('model_task', model_task)
    graph.add_edge(START, 'model_task')
    names = ('embed_user_input', 'embed_intermediate_outputs', 'embed_final_output')
    for name, function in zip(names, (embed_user_input, embed_intermediate_outputs, embed_final_output), strict=True):
        graph.add_node(name, function)
        graph.add_edge(name, 'collect_task_retrieval')
    graph.add_node('collect_task_retrieval', collect_task_retrieval)
    # 三路深度一致，在同一superstep完成后只收束一次；空任务直接进入尾节点。
    graph.add_conditional_edges('model_task', active_branches, [*names, 'collect_task_retrieval'])
    graph.add_edge('collect_task_retrieval', END)
    return graph.compile()


__all__ = ['build_conversation_memory_graph']
