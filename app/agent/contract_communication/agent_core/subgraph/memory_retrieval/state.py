"""公开输入输出与内部检索状态分离。"""
from typing_extensions import TypedDict

from .schema import MemoryRetrievalRequest, MemoryRetrievalResult, RetrievalPlan


class MemoryRetrievalInput(TypedDict):
    request: MemoryRetrievalRequest | dict


class MemoryRetrievalOutput(TypedDict):
    result: MemoryRetrievalResult


class MemoryRetrievalState(MemoryRetrievalInput, total=False):
    validated: MemoryRetrievalRequest
    plan: RetrievalPlan
    error_code: str
    error: str
    result: MemoryRetrievalResult
