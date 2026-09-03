"""合同文档识别的稳定模型输入入口。"""

from app.agent.contract_document_detection.prompt.detection import (
    CONTRACT_DOCUMENT_DETECTION_PROMPT_VERSION,
    CONTRACT_DOCUMENT_DETECTION_TASK_PROMPT,
    CONTRACT_DOCUMENT_DETECTION_TOOL_INSTRUCTION_PROMPT,
    CONTRACT_DOCUMENT_DETECTION_TOOL_PLACEMENT,
    ContractDocumentDetectionPromptVersion,
    build_contract_document_detection_messages,
)

__all__ = [
    "CONTRACT_DOCUMENT_DETECTION_PROMPT_VERSION",
    "CONTRACT_DOCUMENT_DETECTION_TASK_PROMPT",
    "CONTRACT_DOCUMENT_DETECTION_TOOL_INSTRUCTION_PROMPT",
    "CONTRACT_DOCUMENT_DETECTION_TOOL_PLACEMENT",
    "ContractDocumentDetectionPromptVersion",
    "build_contract_document_detection_messages",
]
