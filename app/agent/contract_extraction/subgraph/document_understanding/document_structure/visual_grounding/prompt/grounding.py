"""单元视觉定位工具循环的提示词与消息构造器。"""

from __future__ import annotations

import json

from app.agent.contract_extraction.state import PDFPromptPage, PreparedPDFPage
from app.agent.contract_extraction.subgraph.document_understanding.document_structure.state import (
    DocumentUnit,
)
from app.agent.contract_extraction.subgraph.document_understanding.document_structure.visual_grounding.tool import (
    LocalizationAnchor,
)
from app.agent.contract_extraction.subgraph.document_understanding.prompt import (
    build_pdf_common_messages,
)
from app.agent.contract_extraction.tool_protocol import TOOL_CALL_XML_INSTRUCTION

UNIT_VISUAL_GROUNDING_PROMPT_VERSION = "unit-visual-grounding-v4"

UNIT_VISUAL_GROUNDING_COMMON_TASK = f"""你负责把一个已经确认的合同语义单元定位到页面图像区域。

任务边界：
1. 输入只包含当前单元涉及的物理页面；每张图像前的“第 N 页”是权威页码标签。
2. 单元的 start、navigation、page_body、end 锚点已经由程序按文档阅读顺序编号。你不得重排、跳过、重复或创造锚点。
3. bbox_2d 使用单页 0～1000 归一化坐标 [x_min, y_min, x_max, y_max]，原点位于左上角。
4. 定位框应覆盖属于当前语义单元的连续内容区域，而不是只紧贴锚点文字。

绘制规则：
1. 必须从最早未覆盖锚点开始调用 draw_bbox；一次只画一个单页框。
2. 同一页的多个连续锚点和它们之间的单元内容可以由一个框共同覆盖，并在 anchor_ids 中按顺序全部列出。
3. 跨页单元分别绘制：起始页覆盖 start 之后的单元内容，中间 page_body 页覆盖该页属于单元的连续内容，结束页覆盖到 end 边界为止。
4. 双栏页面遵循实际阅读顺序。可以分别绘制左栏和右栏；从左栏底部进入右栏顶部时允许 y 坐标回到上方。
5. 不要为了追求紧边界遗漏标题、表格、备注、签章或跨栏内容；也不要覆盖明显属于相邻单元的大块区域。

工具协议：
1. 可调用 think 简洁分析下一框；不得连续思考而不推进定位。
2. draw_bbox 成功后根据工具反馈继续处理剩余锚点；错误时按反馈修正。
3. 只有所有锚点都被成功定位后才能调用 finish。每轮必须且只能调用一个工具。
4. {TOOL_CALL_XML_INSTRUCTION}
"""


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def build_unit_visual_grounding_messages(
    pages: tuple[PreparedPDFPage, ...],
    prompt_pages: tuple[PDFPromptPage, ...],
    unit: DocumentUnit,
    anchors: tuple[LocalizationAnchor, ...],
) -> list[dict[str, object]]:
    """只注入当前单元涉及的带页码页面，并在工具之后追加动态目标。"""
    messages = build_pdf_common_messages(pages, prompt_pages)
    messages.append(
        {
            "role": "user",
            "content": UNIT_VISUAL_GROUNDING_COMMON_TASK,
        }
    )
    target = {
        "unit_id": unit.unit_id,
        "label": unit.decision.label,
        "summary": unit.decision.summary,
        "span": unit.decision.span.model_dump(mode="json"),
    }
    messages.append(
        {
            "role": "user",
            "content": (
                f"当前目标单元：\n{_compact_json(target)}\n"
                f"有序定位锚点：\n"
                f"{_compact_json([anchor.model_dump(mode='json') for anchor in anchors])}\n"
                "请从最早未覆盖锚点开始定位。"
            ),
        }
    )
    return messages


__all__ = [
    "UNIT_VISUAL_GROUNDING_COMMON_TASK",
    "UNIT_VISUAL_GROUNDING_PROMPT_VERSION",
    "build_unit_visual_grounding_messages",
]
