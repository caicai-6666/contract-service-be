"""压缩子 Agent 专用工具：工作区增删改与完成。"""
from app.agent.contract_communication.agent_core.tool.workspace import build_workspace_tools
from .finish import FinishCompressionArguments, build_finish_compression_tool


def build_compression_tools() -> list[dict]:
    """每次构造独立定义，压缩推理由模型原生通道承担。"""
    return [*build_workspace_tools(), build_finish_compression_tool()]


__all__ = ['FinishCompressionArguments', 'build_compression_tools']
