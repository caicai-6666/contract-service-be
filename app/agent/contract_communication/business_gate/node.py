"""业务门禁子图的初始化节点。"""

from app.agent.contract_communication.business_gate.state import BusinessGateSubgraphState


def initialize_business_gate(
    state: BusinessGateSubgraphState,
) -> BusinessGateSubgraphState:
    """返回明确的未实现状态，不读取文件、不调用模型、不执行权限判断。"""
    # 始终重新生成状态，不沿用调用方传入的结论，避免占位节点被误用为放行节点。
    return {"status": "not_implemented"}


__all__ = ["initialize_business_gate"]
