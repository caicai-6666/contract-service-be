"""每主题独立输入，结果通过 reducer 汇总，私有状态不泄漏为最终摘要。"""
import operator
from typing import Annotated
from typing_extensions import TypedDict
from ..compression import FIFOCompressionRequest
from .schema import TopicPlan, TopicGenerationRequest, TopicMapResult, FIFOSummaryResult


class SummaryInput(TypedDict):
    request: FIFOCompressionRequest | dict


class SummaryOutput(TypedDict):
    result: FIFOSummaryResult


class TopicMapInput(TypedDict):
    topic_request: TopicGenerationRequest


class SummaryState(SummaryInput, total=False):
    validated: FIFOCompressionRequest
    planning_audit: list[dict]
    generation_audit: Annotated[list[dict], operator.add]
    plan: TopicPlan
    topic_results: Annotated[list[TopicMapResult], operator.add]
    error_type: str
    error: str
    result: FIFOSummaryResult
