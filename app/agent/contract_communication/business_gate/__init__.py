"""合同沟通业务门禁子图装配入口。"""

from langgraph.graph import END, START, StateGraph

from app.agent.contract_communication.business_gate.node import initialize_business_gate
from app.agent.contract_communication.business_gate.state import BusinessGateSubgraphState


def build_business_gate_subgraph():
    """装配业务门禁初始化子图，当前不执行实际准入校验。"""
    graph = StateGraph(BusinessGateSubgraphState)
    graph.add_node("initialize_business_gate", initialize_business_gate)
    graph.add_edge(START, "initialize_business_gate")
    graph.add_edge("initialize_business_gate", END)
    return graph.compile()


__all__ = ["BusinessGateSubgraphState", "build_business_gate_subgraph"]
