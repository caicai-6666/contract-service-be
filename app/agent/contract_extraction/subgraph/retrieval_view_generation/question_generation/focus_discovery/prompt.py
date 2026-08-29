"""合同检索问题关注点逐项发现任务。"""

from __future__ import annotations

from typing import Any, Final

from app.agent.contract_extraction.context import append_contract_task
from app.agent.contract_extraction.subgraph.retrieval_view_generation.question_generation.state import (
    QuestionGenerationContext,
)
from app.agent.contract_extraction.tool_protocol import TOOL_CALL_XML_INSTRUCTION

QUESTION_FOCUS_DISCOVERY_PROMPT_VERSION: Final = "retrieval-question-focus-v1"
QUESTION_FOCUS_DISCOVERY_TOOL_PLACEMENT: Final = "before_task"

_QUESTION_FOCUS_DISCOVERY_TASK_BASE = """你已获得当前合同的完整原始 PDF、权威文档导航结构、已确认分类结果和完整提问指南。当前任务是按真实用户检索价值从高到低，逐个发现值得用于生成正式问题的关注点要求；现在不生成正式问题或答案。

事实与选择边界：
1. 原始 PDF 是合同事实的唯一权威来源；文档导航结构和分类结果只帮助定位与理解，不能替代页面核查。
2. 提问指南是候选方向和判断规则，不证明当前合同已经具备相应事项；必须先核查合同证据与适用性。
3. 合同已有明确约定，或者事项适用但合同可能存在会实质影响交易的重要缺失，都可以形成关注点；明显不适用、纯结构过滤或没有真实查询价值的事项不得提交。
4. 每次优先提交尚未记录事项中，对合同目的、交易内容、履行完成、付款结算、交付验收、风险承担或责任救济影响最大的关注点；低价值事项后置。
5. 一个关注点可以融合多个指南标识，但这些事项必须属于同一真实用户意图，并能由一个自然、连贯的问题共同询问；不能为了减少条目而强行合并不同业务目的。
6. 语义相同、包含关系明显或可以由同一答案覆盖的关注点只保留一个。提交前必须回顾当前成功工具轨迹，避免重复。
7. focus 只描述后续问题必须询问的对象、范围、条件、时间、金额、责任或例外，不直接写成用户问题，不提前回答，也不要求合同外法律分析、效力判断、争议预测或行动建议。

动作要求：
1. 首轮必须且只能调用 think，先扫描合同主题、通用关注点和可能适用的领域关注点，形成从高价值事项开始发现的思路；首轮不得提交关注点或结束。
2. 首轮思考成功后，每轮必须且只能调用一个当前提供的工具：可以继续 think、用 generate_question_focus 提交一个关注点，或用 finish_question_focus_discovery 自然结束。
3. generate_question_focus 必须按“合同证据 → 简洁推理摘要 → 指南标识 → 最终关注点要求”提交；证据证明事项适用或存在重要缺失的可能性，推理说明混合事项为何属于同一用户意图。
4. 每次成功的思考、关注点和工具反馈都会保留在当前对话轨迹中。错误动作及反馈在下一次动作正确完成后会被清除，不作为后续判断依据。
5. 只有确认不存在尚未记录且值得真实用户独立检索的关注点时，才调用 finish_question_focus_discovery。工具参数、页码、指南标识或业务规则不合法时，根据短反馈修正；普通文本和伪造工具格式不能作为结果。"""

QUESTION_FOCUS_DISCOVERY_TASK: Final = (
    f"{_QUESTION_FOCUS_DISCOVERY_TASK_BASE}\n\n工具调用格式：\n"
    f"{TOOL_CALL_XML_INSTRUCTION}"
)


def build_question_focus_discovery_messages(
    context: QuestionGenerationContext,
) -> list[dict[str, Any]]:
    """在无数量提示的提问指南上下文尾部追加稳定发现任务。"""
    return append_contract_task(
        context.messages,
        task_suffix=QUESTION_FOCUS_DISCOVERY_TASK,
    )


__all__ = [
    "QUESTION_FOCUS_DISCOVERY_PROMPT_VERSION",
    "QUESTION_FOCUS_DISCOVERY_TASK",
    "QUESTION_FOCUS_DISCOVERY_TOOL_PLACEMENT",
    "build_question_focus_discovery_messages",
]
