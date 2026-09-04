"""合同基础前缀与最终公共前缀的确定性组装函数。"""

from __future__ import annotations

import json
from collections.abc import Iterable
from copy import deepcopy
from hashlib import sha256
from typing import TYPE_CHECKING, Any

import yaml

from app.agent.contract_extraction.state import PDFPromptPage, PreparedPDFPage
from app.agent.contract_extraction.subgraph.document_understanding.document_structure.state import (
    DocumentStructureMetadata,
)
from app.agent.contract_extraction.subgraph.document_understanding.prompt import (
    build_pdf_common_messages,
)

if TYPE_CHECKING:
    from app.agent.contract_extraction.subgraph.classification.state import (
        ContractClassificationResult,
    )

CONTRACT_BASE_CONTEXT_VERSION = "contract-base-context-v4"
CONTRACT_PREFILL_CONTEXT_VERSION = "contract-prefill-context-v6"

DOCUMENT_STRUCTURE_COMMENTS = """# 权威文档导航结构；用于理解和定位，合同页面图像仍是最终事实来源。
# 字段说明：
# scope：整份合同的整体认识。
# units：按原文顺序排列的宏观连续内容单元。
# unit_locations：各单元的单页视觉区域；failed 只表示定位失败，不否定单元内容。
# evidence：带物理页码的可核对证据。
# reasoning_summary：证据如何支持当前结构决定的简洁说明。
# decision：整体主题或单元边界的最终决定。"""

CLASSIFICATION_COMMENTS = """# 已确认的合同分类结果；用于理解交易场景和适用规则，合同页面图像仍是最终事实来源。
# 字段说明：
# status：classified 表示至少命中一类；unmapped 表示完整判别后未命中；partial/failed 表示判别不完整。
# matches：全部命中类别；同一合同可以命中多个类别。
# evidence：支持命中判断的页码与原文证据。
# reasoning_summary：证据如何满足类别定义的简洁说明。
# decision：类别编码、名称和当前合同交易场景。
# unmapped_type_description：仅 unmapped 且兜底成功时出现，是类型描述而非新类别码。"""


class _IndentedSafeDumper(yaml.SafeDumper):
    """让 YAML 列表相对父键缩进，保持复杂结构易读。"""

    def increase_indent(
        self,
        flow: bool = False,
        indentless: bool = False,
    ) -> None:
        del indentless
        super().increase_indent(flow, False)


def _serialize_commented_yaml(
    *,
    comments: str,
    root_key: str,
    payload: Any,
) -> str:
    """生成字段顺序稳定、包含说明注释的模型可读 YAML。"""
    serialized = yaml.dump(
        {root_key: payload},
        Dumper=_IndentedSafeDumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=1000,
    ).rstrip()
    return f"{comments.rstrip()}\n{serialized}"


def context_sha256(messages: Iterable[dict[str, Any]]) -> str:
    """计算应用层消息前缀的确定性指纹。"""
    serialized = json.dumps(
        list(messages),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(serialized).hexdigest()


def build_contract_base_messages(
    pages: Iterable[PreparedPDFPage],
    prompt_pages: Iterable[PDFPromptPage],
    structure: DocumentStructureMetadata,
) -> list[dict[str, Any]]:
    """复用页面图像公共前缀，并在末尾稳定追加权威文档结构。"""
    messages = build_pdf_common_messages(pages, prompt_pages)
    content = messages[-1]["content"]
    if not isinstance(content, list):
        raise TypeError("页面图像公共 user 消息必须使用内容块列表")
    structure_data = structure.model_dump(mode="json", exclude={"document_id"})
    content.append(
        {
            "type": "text",
            "text": _serialize_commented_yaml(
                comments=DOCUMENT_STRUCTURE_COMMENTS,
                root_key="document_structure",
                payload=structure_data,
            ),
        }
    )
    return messages


def build_contract_prefill_messages(
    base_messages: Iterable[dict[str, Any]],
    classification: ContractClassificationResult,
) -> list[dict[str, Any]]:
    """复制基础前缀，并在末尾追加分类结果的模型可读投影。"""
    messages = deepcopy(list(base_messages))
    if not messages:
        raise ValueError("合同基础前缀不能为空")
    content = messages[-1].get("content")
    if not isinstance(content, list):
        raise TypeError("合同基础 user 消息必须使用内容块列表")
    # 模型只需要分类语义；模型名、提示词版本和目录指纹保留在工作流状态中
    # 用于审计，避免把无助于下游推理的动态元数据写入长公共前缀。
    classification_data = classification.model_dump(mode="json")
    classification_payload = {
        "status": classification_data["status"],
        "matches": classification_data["matches"],
    }
    if classification_data["unmapped_type_description"] is not None:
        classification_payload["unmapped_type_description"] = (
            classification_data["unmapped_type_description"]
        )

    content.append(
        {
            "type": "text",
            "text": _serialize_commented_yaml(
                comments=CLASSIFICATION_COMMENTS,
                root_key="classification",
                payload=classification_payload,
            ),
        }
    )
    return messages


def append_contract_task(
    common_messages: Iterable[dict[str, Any]],
    *,
    task_suffix: str,
) -> list[dict[str, Any]]:
    """复制公共前缀后追加任务，供模型节点统一复用。"""
    if not task_suffix.strip():
        raise ValueError("task_suffix 不能为空")
    messages = deepcopy(list(common_messages))
    if not messages:
        raise ValueError("下游公共前缀不能为空")
    content = messages[-1].get("content")
    if not isinstance(content, list):
        raise TypeError("下游公共 user 消息必须使用内容块列表")
    content.append(
        {
            "type": "text",
            "text": f"任务：\n{task_suffix.strip()}",
        }
    )
    return messages
