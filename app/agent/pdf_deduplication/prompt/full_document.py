"""两份合同全部可用页面同时提供时的关系判断策略提示词。"""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from typing import Any, Final, Literal

from app.agent.contract_extraction.state import PDFPromptPage, PreparedPDF
from app.agent.contract_extraction.subgraph.document_understanding.prompt import (
    build_pdf_common_messages,
    build_pdf_content_blocks,
    build_pdf_page_descriptor,
)
from app.agent.contract_extraction.tool_protocol import TOOL_CALL_XML_INSTRUCTION
from app.agent.pdf_deduplication.prompt.relation_standard import (
    CANDIDATE_CONTRACT_END_DIVIDER,
    CANDIDATE_CONTRACT_INPUT_HEADER,
    TOOL_INSTRUCTION_START_DIVIDER,
    append_contract_relation_standard,
)

FullDocumentJudgmentPromptVersion = Literal[
    "full-document-relation-judgment-v3"
]

FULL_DOCUMENT_JUDGMENT_PROMPT_VERSION: Final[
    FullDocumentJudgmentPromptVersion
] = "full-document-relation-judgment-v3"

# 工具必须紧随最后一条候选 PDF 任务消息，由 vLLM 的聊天模板渲染真实
# Pydantic function schema；不能把 schema 手工复制进提示词。
FULL_DOCUMENT_TOOL_PLACEMENT: Final[Literal["after_task"]] = "after_task"

FULL_DOCUMENT_JUDGMENT_STRATEGY_PROMPT: Final = """本次比较严格使用共同标准已经定义的文档身份：“上传合同 A”是前面已经提供的第一组 PDF 页面，“候选合同 B”是本任务之后提供的第二组 PDF 页面。A、B 各自包含的全部可用页面均会提供，物理页码在两份 PDF 内分别从 1 开始；“全部可用页面”只表示当前文件包含的全部页面，不保证原合同没有缺页、遮挡、模糊或扫描不完整。

当前任务：
按照已经提供的合同关系共同判断标准，判断两份 PDF 的关系。两份 PDF 当前包含的页面会一次性全部提供，不存在可以继续请求的其他页面。

全量查看要求：
1. 按各自物理页码顺序核对两份 PDF 的全部页面，先确认每份 PDF 实际代表的文件范围，再进行跨文档比较。
2. 不得只根据首页、尾页、合同编号、签约主体、印章或视觉排版中的单一线索提前决定关系。
3. 必须综合核对合同身份、交易事项、版本连续性、关键条款、签署信息、页面完整性和视觉结构。
4. 发现金额、日期、条款、页面或签章差异时，必须继续判断该差异属于同一合同的修订或替换，还是应独立保留的关联文件或不同合同。
5. 页面数量或顺序不同不能直接决定关系；应结合共有页面、缺失内容、新增内容和文件范围判断。
6. 不得虚构未提供的页面、文字、数字、表格、签章或版本关系，也不得声称需要查看当前材料之外的页面。"""

FULL_DOCUMENT_TOOL_INSTRUCTION_PROMPT: Final = f"""工具使用：
1. 每轮必须且只能调用一个当前提供的工具，不得输出普通文本或用代码块、工具名加 JSON 等文本模拟工具调用。
2. think 是允许进行实际分析和推理的工作空间。你可以在 reasoning 中比较证据、建立和排除关系假设、分析版本连续性与冲突，并判断下一步动作；包含工具结构在内的整轮响应最多使用 1024 completion tokens，think 不提交正式关系。
3. think 可以按需调用，不要求为了形式固定调用。不得连续调用超过两次；完成思考后应根据证据调用终止工具，而不是无界继续推理。
4. 能够可靠判断时，调用 submit_contract_relation。提交内容必须依次给出跨文档页面证据、简洁推理摘要和 duplicate、similar、different 中唯一一个最终关系。
5. duplicate 的证据必须支持合同身份连续、文件同源或版本替代，不能只依赖版式相似。similar 的证据必须同时说明两份文件为何相关或高度相似，以及为何仍应独立保存。different 的证据必须说明支持合同身份、交易事项或文件范围独立的关键差异。
6. 只有页面不可读、关键证据缺失或冲突无法消解，导致三种关系均不能由充分证据支持时，才可调用 report_unable_to_determine_relation。调用前必须至少完成一次 think，核对无法判断的具体原因；不得因为比较复杂、页面较多或尚未认真检查全部页面而放弃。
7. think、正式提交和无法判断出口都是互斥的单次工具动作；任何一轮调用工具后都不得追加说明文字。

{TOOL_CALL_XML_INSTRUCTION}"""


def append_full_document_judgment_strategy(
    relation_standard_messages: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """在上传 PDF 与共同标准之后追加稳定的全量查看任务。"""
    messages = deepcopy(list(relation_standard_messages))
    if not messages:
        raise ValueError("合同关系共同判断消息不能为空")
    content = messages[-1].get("content")
    if not isinstance(content, list):
        raise TypeError("共同判断消息的最后一条消息必须使用内容块列表")
    content.append(
        {
            "type": "text",
            "text": (
                "两份合同全部可用页面的查看策略：\n"
                f"{FULL_DOCUMENT_JUDGMENT_STRATEGY_PROMPT}"
            ),
        }
    )
    return messages


def append_full_document_candidate_pdf(
    full_document_messages: Iterable[dict[str, Any]],
    candidate_pdf: PreparedPDF,
) -> list[dict[str, Any]]:
    """追加候选 PDF，并以工具分隔线结束最后一个真实 user 任务。"""
    messages = deepcopy(list(full_document_messages))
    if not messages:
        raise ValueError("全量判断消息不能为空")

    prompt_pages = tuple(
        PDFPromptPage(
            page_number=page.page_number,
            width_pixels=page.width_pixels,
            height_pixels=page.height_pixels,
            descriptor=build_pdf_page_descriptor(page),
        )
        for page in candidate_pdf.pages
    )
    content = build_pdf_content_blocks(
        candidate_pdf.pages,
        prompt_pages,
        header=CANDIDATE_CONTRACT_INPUT_HEADER,
    )
    content.append(
        {
            "type": "text",
            "text": (
                f"{CANDIDATE_CONTRACT_END_DIVIDER}\n"
                "以上全部页面属于“候选合同 B”。\n\n"
                f"{TOOL_INSTRUCTION_START_DIVIDER}\n"
                f"{FULL_DOCUMENT_TOOL_INSTRUCTION_PROMPT}"
            ),
        }
    )
    messages.append({"role": "user", "content": content})
    return messages


def build_full_document_judgment_messages(
    uploaded_pdf: PreparedPDF,
    candidate_pdf: PreparedPDF,
) -> list[dict[str, Any]]:
    """构建“上传 PDF → 判断任务 → 候选 PDF → 工具说明”的完整消息。"""
    uploaded_prompt_pages = tuple(
        PDFPromptPage(
            page_number=page.page_number,
            width_pixels=page.width_pixels,
            height_pixels=page.height_pixels,
            descriptor=build_pdf_page_descriptor(page),
        )
        for page in uploaded_pdf.pages
    )
    uploaded_messages = build_pdf_common_messages(
        uploaded_pdf.pages,
        uploaded_prompt_pages,
    )
    relation_messages = append_contract_relation_standard(uploaded_messages)
    strategy_messages = append_full_document_judgment_strategy(relation_messages)
    return append_full_document_candidate_pdf(strategy_messages, candidate_pdf)


__all__ = [
    "FULL_DOCUMENT_JUDGMENT_PROMPT_VERSION",
    "FULL_DOCUMENT_JUDGMENT_STRATEGY_PROMPT",
    "FULL_DOCUMENT_TOOL_INSTRUCTION_PROMPT",
    "FULL_DOCUMENT_TOOL_PLACEMENT",
    "FullDocumentJudgmentPromptVersion",
    "append_full_document_candidate_pdf",
    "append_full_document_judgment_strategy",
    "build_full_document_judgment_messages",
]
