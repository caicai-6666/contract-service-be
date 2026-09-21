from typing_extensions import TypedDict
from .schema import ContractRetrievalRequest,ContractRetrievalResult,QueryAlignmentGeneration

class ContractRetrievalInput(TypedDict):
    request: ContractRetrievalRequest|dict
class ContractRetrievalOutput(TypedDict):
    result: ContractRetrievalResult
class ContractRetrievalState(ContractRetrievalInput,total=False):
    alignment_passed: bool
    aligned_request: ContractRetrievalRequest
    alignment: QueryAlignmentGeneration
    result: ContractRetrievalResult

