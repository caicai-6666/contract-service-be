"""压缩完成工具：只申请结束，不提交或替换工作区内容。"""
from typing import Annotated
from pydantic import Field, StringConstraints
from app.schema.communication_workspace import WorkspaceObject


class FinishCompressionArguments(WorkspaceObject):
    summary: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=1000)] = Field(
        description='简短说明本次精简的内容以及保留的关键事实和保护约束；不提交工作区全文或思维链，不宣称已持久化。最多1000字符。只有副本实际使用率低于70%时允许完成。')


def build_finish_compression_tool() -> dict:
    return {'type': 'function', 'function': {
        'name': 'finish_compression', 'description': '申请完成本次工作区压缩。系统校验当前副本严格低于70%且保护约束满足后才接受；完成摘要不替代工作区，未达标时必须继续整理。',
        'parameters': FinishCompressionArguments.model_json_schema(), 'strict': False,
    }}
