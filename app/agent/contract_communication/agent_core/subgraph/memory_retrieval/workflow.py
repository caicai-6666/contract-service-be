"""单节点多轮记忆检索子图；查询执行为方法，不另建子图。"""
from functools import partial
from langgraph.graph import END, START, StateGraph
from .node import retrieve_memory
from .state import MemoryRetrievalInput, MemoryRetrievalOutput, MemoryRetrievalState


def build_memory_retrieval_subgraph(*, result_pool, **options):
    graph=StateGraph(MemoryRetrievalState,input_schema=MemoryRetrievalInput,output_schema=MemoryRetrievalOutput)
    graph.add_node('retrieve_memory',partial(retrieve_memory,result_pool=result_pool,**options))
    graph.add_edge(START,'retrieve_memory')
    graph.add_edge('retrieve_memory',END)
    return graph.compile()
