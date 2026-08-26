"""最终公共前缀预热子图状态。"""

from typing_extensions import TypedDict

from app.agent.contract_extraction.state import (
    ContractBaseContext,
    ContractPrefillContext,
    ContractPreheatResult,
)
from app.agent.contract_extraction.subgraph.classification.state import (
    ContractClassificationResult,
)


class PreheatSubgraphState(TypedDict, total=False):
    """两个预热节点之间传递的私有状态。"""

    base_context: ContractBaseContext
    classification: ContractClassificationResult
    prefill_context: ContractPrefillContext
    preheat: ContractPreheatResult
