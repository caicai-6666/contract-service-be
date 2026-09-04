"""合同建议文件名生成任务的确定性提示词。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.agent.contract_extraction.state import ContractBaseContext
from app.agent.contract_extraction.subgraph.classification.state import (
    ContractClassificationResult,
)
from app.agent.contract_extraction.tool_protocol import TOOL_CALL_XML_INSTRUCTION

FILE_NAME_GENERATION_PROMPT_VERSION = "file-name-generation-v6"

_STATUS_LABELS = {
    "classified": "完整分类；以下分组是全部已确认命中的类别。",
    "partial": (
        "部分分类；以下分组仅是已经确认命中的类别，不能视为完整类别集合。"
    ),
    "unmapped": "完整分类但未命中任何已知类别。",
}

FILE_NAME_GENERATION_TASK = f"""你已获得当前合同按原始顺序排列的页面图像、权威文档导航结构，以及只保留命名所需信息的合同分类摘要。

任务目标：
为当前合同生成一个准确、简洁、便于用户识别和修改的展示文件名。名称应让用户不打开文件也能大致知道“这是关于什么的哪类合同”，而不是简单抄录缺乏辨识度的原始标题；只生成名称主体，不附加文件扩展名。

资料边界：
1. 合同页面图像是合同标题、主体、标的和其他事实的唯一来源。
2. 文档导航结构只用于定位标题与核心交易内容，不得替代页面证据。
3. 分类摘要只用于理解合同类别和交易场景；分类状态为 partial 时，已列类别仍可辅助命名，但不得把该列表理解为完整分类结果。
4. 不使用上传时的原始文件名推断合同事实，不把分类摘要中的概括误写成页面原文。

命名方法：
1. 先核查封面、首页标题区、正文开头和页眉中的正式标题，再核查标的名称、项目名称、具体商品、服务内容或授权事项；正式标题只是命名素材，不是必须照抄的最终名称。
2. 判断正式标题是否具有足够辨识度。只有标题已经明确包含核心标的、具体项目或实际服务内容，并且与正文一致时，才优先沿用；可以删除不影响核心内容的冗长主体全称，只修正不适合作为文件名的空白和符号，不为了改写而替换准确标题中的业务短语。
3. “合同”“合同书”“协议”“协议书”“买卖合同”“采购合同”“销售合同”“服务合同”“合作协议”“租赁合同”等仅表达通用文种或交易类别的标题，都属于泛化标题，不能直接作为建议名称。
4. 标题泛化或缺失时，优先采用“核心标的、具体项目或实际服务内容 + 合同类型”的结构重新凝练。合同类型可以来自页面标题、正文关系或分类摘要；核心内容必须由页面事实支持，不能只从类别名称推断。
5. 页面已经给出明确的商品、设备、项目或服务名称时，把这个名称视为不可随意拆分的核心事实短语。建议名称应优先原样保留其中决定对象含义的构成词，尤其是中心词；不得为了简短而删除、替换、缩写或上位概括后导致交易对象发生变化。空白、分隔符等版式字符可以规范化。
6. 单一核心标的可以省略型号、规格、数量、价格和完整清单，但必须保留准确的标的名称。涉及多项内容时，只能使用页面明确出现的上位概称；页面没有可靠概称时，突出主要标的，不自行创造概括词，也不把具体对象泛化成类别名称。
7. 主体名称、日期、合同编号和项目编号通常不是标题主体；只有核心内容仍不足以区分，且页面明确记载这些信息时，才选择最必要的一项辅助区分。删除主体名称后必须重新检查剩余名称是否完整、自然且没有损伤核心事实短语。
8. 存在多个命中类别时，名称应突出整份合同的主要交易内容；只有并列交易对识别合同确有必要时，才组合多个类别，不机械拼接全部类别名称。
9. 页面不足以支持更具体的核心内容时，宁可保留可确认的交易类型，也不得为了显得友好而虚构标的、项目、服务或主体。

文件名要求：
1. file_name 只表示展示文件名，不是存储路径，也不是合同唯一标识。
2. 使用简洁中文，避免宣传语、评价、解释、版本推测和无助于识别的冗余信息。
3. 只生成名称主体，不得附加任何文件扩展名。
4. 不得包含 `/`、`\\`、`:`、`*`、`?`、`\"`、`<`、`>`、`|` 或换行，不得以空格或句点开头、结尾。
5. 除非合同页面明确记载且有助于区分合同，否则不添加日期、合同编号、项目编号或主体简称。

虚构判断示例（只说明规则，不代表当前合同事实）：
- 页面标题为“销售合同”，商品栏明确写有“防爆压力变送器”：建议 `防爆压力变送器销售合同`；不建议 `防爆压力变送销售合同`，因为删除中心词“器”改变了交易对象。
- 页面标题为“服务协议”，正文明确服务是“办公楼中央空调年度维护保养”：建议 `办公楼中央空调年度维护保养服务协议`；不建议 `办公楼设备服务协议`，因为后者擅自把具体服务泛化了。
- 页面标题为“甲方公司滨江仓储园区消防设施改造工程合同”，且与正文一致：可删除冗长主体前缀，建议 `滨江仓储园区消防设施改造工程合同`，但必须完整保留具体项目名称。
- 页面列有多项物料，同时明确用“实验室耗材”统称这些标的：可建议 `实验室耗材采购合同`；若页面没有这个统称，不得自行创造该概括词。
- 页面没有正式标题，分类摘要显示多个类别：仍应根据页面中的主要交易内容命名，不输出类别名称的机械串联。

工具与输出协议：
1. 每轮必须且只能调用一个当前提供的工具，禁止输出普通文本或模拟工具调用。
2. `think` 用于基于页面、文档结构和分类摘要进行实际命名分析；可以按需调用，但不得连续调用超过两次。其 `reasoning` 不提交正式名称，证据充分时应立即调用终止工具。
3. `submit_suggested_file_name` 是唯一终止工具，必须按 `evidence → reasoning → file_name` 的顺序提交页面证据、简洁命名理由和唯一建议名称。
4. `evidence` 只引用物理页码和足以核对名称的短原文。原始标题泛化或缺失时，必须提供能够支持建议名称中核心标的、具体项目或实际服务内容的页面证据。
5. `reasoning` 应说明原始标题是否泛化，以及页面事实和分类摘要如何支持凝练后的核心内容与合同类型；还应明确核对是否完整保留了页面中的核心事实短语，不输出探索过程，不引入证据外事实。
6. 提交前逐词比较 `file_name` 的核心内容与 `evidence` 原文：如果删除某个构成词会使商品、设备、项目或服务变成不同对象，必须恢复该词；最终名称中的业务事实都必须能由证据直接核对。
7. `file_name` 只提交最终名称主体，不附加文件扩展名，不填写解释或备选名称。
8. 工具返回 `ok=false` 时，根据反馈指出的字段和修正方向重新调用；失败内容不得作为正式结果。
9. `think` 与正式提交是互斥的单次工具动作；任何一轮调用工具后都不得追加说明文字。

工具调用格式：
{TOOL_CALL_XML_INSTRUCTION}"""


def _inline_markdown_value(value: str, *, field_name: str) -> str:
    """折叠动态文本的空白，防止内容破坏固定 Markdown 分组。"""
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError(f"{field_name} 不能为空")
    return normalized


def render_classification_summary(
    classification: ContractClassificationResult,
) -> str:
    """按原有命中顺序渲染名称生成所需的精简分类 Markdown。"""
    if classification.status == "failed":
        raise ValueError("分类全部失败时不能组装建议文件名上下文")
    if classification.status == "classified" and not classification.matches:
        raise ValueError("classified 分类结果必须至少包含一个命中类别")
    if classification.status == "unmapped" and classification.matches:
        raise ValueError("unmapped 分类结果不能包含命中类别")

    lines = [
        "## 已判定的合同分类",
        "",
        f"- 分类状态：{_STATUS_LABELS[classification.status]}",
    ]

    if classification.status == "unmapped":
        description = classification.unmapped_type_description
        if description is not None:
            lines.append(
                "- 未映射类型说明："
                + _inline_markdown_value(
                    description,
                    field_name="unmapped_type_description",
                )
            )
        else:
            lines.append("- 未映射类型说明：未形成可靠说明，请直接核查合同页面。")
        return "\n".join(lines)

    if not classification.matches:
        lines.extend(("", "当前没有已经确认命中的类别，请直接核查合同页面。"))
        return "\n".join(lines)

    for index, match in enumerate(classification.matches, start=1):
        lines.extend(
            (
                "",
                f"### 命中类别 {index}",
                "",
                "- 类别名称："
                + _inline_markdown_value(
                    match.decision.category_name,
                    field_name="category_name",
                ),
                "- 判定理由："
                + _inline_markdown_value(
                    match.reasoning_summary,
                    field_name="reasoning_summary",
                ),
                "- 实际场景："
                + _inline_markdown_value(
                    match.decision.scenario,
                    field_name="scenario",
                ),
            )
        )
    return "\n".join(lines)


def build_file_name_generation_messages(
    base_context: ContractBaseContext,
    classification: ContractClassificationResult,
) -> list[dict[str, Any]]:
    """复用页面图像与合同结构前缀，追加分类摘要和命名任务。"""
    if base_context.document_id != classification.document_id:
        raise ValueError("合同基础上下文与分类结果的 document_id 不一致")

    messages = deepcopy(list(base_context.messages))
    if not messages:
        raise ValueError("建议文件名生成的合同基础上下文不能为空")
    content = messages[-1].get("content")
    if not isinstance(content, list):
        raise TypeError("合同基础 user 消息必须使用内容块列表")

    content.extend(
        (
            {
                "type": "text",
                "text": render_classification_summary(classification),
            },
            {
                "type": "text",
                "text": f"任务：\n{FILE_NAME_GENERATION_TASK}",
            },
        )
    )
    return messages


__all__ = [
    "FILE_NAME_GENERATION_PROMPT_VERSION",
    "FILE_NAME_GENERATION_TASK",
    "build_file_name_generation_messages",
    "render_classification_summary",
]
