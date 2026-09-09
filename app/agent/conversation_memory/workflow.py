"""筛选→Send并发整理与编码→待入库汇总；不负责应用调度或SQLite写入。"""

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.agent.conversation_memory.node import plan_memory_tasks, process_memory_tasks, collect_memory_results
from app.agent.conversation_memory.tool import MemorySelectionResult
from app.core.config import get_settings
from app.agent.conversation_memory.state import (
    ConversationMemoryInputState, ConversationMemoryOutputState, ConversationMemoryState,
)


def dispatch_memory_tasks(state: ConversationMemoryState):
    """每个Send只带一份任务和指导；没有选择时也进入汇总，返回明确空结果。"""
    if state['execution_status'] == 'failed':
        return END
    plans = MemorySelectionResult.model_validate(state['plans'])
    tasks = {task.task_id: task for task in state['request'].tasks}
    if not plans.selected_tasks:
        return 'collect_memory_results'
    return [Send('process_memory_tasks', {
        'task': tasks[item.task_id].model_copy(deep=True), 'selection': item,
    }) for item in plans.selected_tasks]


def build_conversation_memory_graph():
    """构建批次独立子图，输入仅接收 request，输出计划及私有执行审计。"""
    graph = StateGraph(
        ConversationMemoryState,
        input_schema=ConversationMemoryInputState,
        output_schema=ConversationMemoryOutputState,
    )
    graph.add_node('plan_memory_tasks', plan_memory_tasks)
    graph.add_node('process_memory_tasks', process_memory_tasks)
    graph.add_node('collect_memory_results', collect_memory_results)
    graph.add_edge(START, 'plan_memory_tasks')
    graph.add_conditional_edges(
        'plan_memory_tasks',
        dispatch_memory_tasks,
        ['process_memory_tasks', 'collect_memory_results', END],
    )
    graph.add_edge('process_memory_tasks', 'collect_memory_results')
    graph.add_edge('collect_memory_results', END)
    # 分支内先整理再编码，以两服务较小配额保守限制整条分支；不新增跨事件循环信号量。
    settings = get_settings()
    return graph.compile().with_config({'max_concurrency': min(
        settings.mllm.max_concurrent_requests, settings.embedding.max_concurrent_requests,
    )})


__all__ = ['build_conversation_memory_graph']
