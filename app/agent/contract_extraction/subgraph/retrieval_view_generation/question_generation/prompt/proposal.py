"""按问题规划并发生成正式问题的公共任务与动态材料。"""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from typing import Any, Final

import yaml

from app.agent.contract_extraction.context import append_contract_task
from app.agent.contract_extraction.state import ContractPrefillContext
from app.agent.contract_extraction.subgraph.retrieval_view_generation.definition import (
    RetrievalViewGuideCatalog,
)
from app.agent.contract_extraction.subgraph.retrieval_view_generation.prompt import (
    render_selected_question_guides,
)
from app.agent.contract_extraction.subgraph.retrieval_view_generation.question_generation.focus_discovery.tool import (
    GeneratedQuestionFocus,
)
from app.agent.contract_extraction.tool_protocol import TOOL_CALL_XML_INSTRUCTION

QUESTION_PROPOSAL_COMMON_PROMPT_VERSION: Final = "retrieval-question-proposal-common-v2"
QUESTION_PROPOSAL_TARGET_PROMPT_VERSION: Final = "retrieval-question-proposal-target-v2"
QUESTION_PROPOSAL_TOOL_PLACEMENT: Final = "before_task"

QUESTION_PLAN_BEGIN: Final = "===== 当前问题规划：开始 ====="
QUESTION_PLAN_END: Final = "===== 当前问题规划：结束 ====="

QUESTION_PLAN_COMMENTS: Final = """# 当前问题规划由先前的合同选题过程生成并经程序校验；一份规划可以组合多个紧密相关的指南关注点。
# 字段说明：
# evidence：支持本规划适用于当前合同的简短页面原文证据；仍须回到合同页面核对，不可把它当成完整答案。
# reasoning_summary：多个指南关注点为何能够由同一个真实用户问题共同覆盖的简洁依据。
# focus_requirement：本次问题必须覆盖的合同对象、范围、条件、时间、金额、责任或例外；它是问题要求，不是预制答案。"""

_QUESTION_PROPOSAL_COMMON_TASK_BASE: Final = """你已获得当前合同按原始顺序排列的完整页面图像、权威文档导航结构和已确认分类结果。你还会收到一份当前问题规划，以及程序根据该规划精确选择的一个或多个权威提问指南关注点。当前任务是把这份问题规划表达为一个贴近真实用户查询的正式问题。

事实与任务边界：
1. 合同页面图像是合同事实的唯一权威来源；文档导航结构和分类结果只帮助定位与理解，不能替代页面核查。
2. 相关提问指南规定适用情形、重点检查和排除边界；当前问题规划规定本次要表达的具体用户意图。不得扩展到未提供的指南事项，也不得重新规划其他问题。
3. 一份问题规划可以有多个紧密相关的指南关注点。应把它们组织成一个连贯的主要意图，可以使用一句话或两个紧密衔接的问句，不得因为指南条目有多个就机械拆分问题。
4. 规划中的 evidence 用于确认选题和定位；生成问题前仍应核对合同页面。不得把规划证据、推理摘要或指南文字直接拼成僵硬问题。
5. 只询问合同如何约定或哪些合同事实成立，不要求合同外法律分析、效力判断、争议预测或行动建议。

人类提问风格：
1. 模拟采购、销售、项目、财务、法务或管理人员真实查找合同时会输入的表达，不写成审查清单、字段标签、指南复述或法律教材问题。
2. 合同页面能够确认时，使用相关方简称、具体产品、服务或项目作为交易锚点；不得猜测提问者属于合同哪一方，也不要机械地以“合同约定的”开头。
3. 优先询问谁做什么、什么时候做、满足什么条件、涉及多少。在同一主要意图内，可以把金额、期限、触发条件和例外一起问清楚。
4. 问题应信息充足、自然且可脱离当前对话理解；长度服从真实查询需要，不为简短而删除主体、交易对象或关键背景。
5. 风格示例：不要写“合同约定的机械臂设备价款分几期支付？各期款项的比例、金额及支付触发条件分别是什么？”，优先写“深圳现象向大肯科技采购的这台机械臂要分几次付款？预付款、发货款和验收尾款分别什么时候付、各付多少？”。示例只说明表达风格，不得把示例事实用于其他合同。

提交要求：
1. 每轮必须且只能调用 propose_question；不需要重新规划、继续思考或结束其他问题。
2. 按“合同证据 → 简洁推理摘要 → 正式问题”的顺序提交。证据应简短且可由合同页面核对；推理摘要说明最终问题如何忠实覆盖当前规划；question 只提交一个连贯问题。
3. 工具参数、页码、证据顺序或业务规则不合法时，只根据短反馈修正当前结果。普通文本和伪造工具格式不能作为结果；错误轨迹在下一次结果通过全部校验后清除。"""

QUESTION_PROPOSAL_COMMON_TASK: Final = (
    f"{_QUESTION_PROPOSAL_COMMON_TASK_BASE}\n\n工具调用格式：\n"
    f"{TOOL_CALL_XML_INSTRUCTION}\n\n当前问题规划 YAML 结构说明：\n"
    f"{QUESTION_PLAN_COMMENTS}"
)


class _IndentedSafeDumper(yaml.SafeDumper):
    """让问题规划 YAML 列表相对父键稳定缩进。"""

    def increase_indent(
        self,
        flow: bool = False,
        indentless: bool = False,
    ) -> None:
        del indentless
        super().increase_indent(flow, False)


def _render_question_plan(focus: GeneratedQuestionFocus) -> str:
    """把一个可组合关注点的问题规划渲染为最小模型投影。"""
    payload = {
        "evidence": [item.model_dump(mode="json") for item in focus.evidence],
        "reasoning_summary": focus.reasoning_summary,
        "focus_requirement": focus.focus,
    }
    serialized = yaml.dump(
        {"current_question_plan": payload},
        Dumper=_IndentedSafeDumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=1000,
    ).rstrip()
    return f"{QUESTION_PLAN_BEGIN}\n{serialized}\n{QUESTION_PLAN_END}"


def build_question_proposal_common_messages(
    prefill_context: ContractPrefillContext,
) -> list[dict[str, Any]]:
    """在合同前缀尾部追加所有并发问题请求共享的表达任务。"""
    return append_contract_task(
        prefill_context.messages,
        task_suffix=QUESTION_PROPOSAL_COMMON_TASK,
    )


def append_question_plan_target(
    common_messages: Iterable[dict[str, Any]],
    *,
    guide_catalog: RetrievalViewGuideCatalog,
    focus: GeneratedQuestionFocus,
) -> list[dict[str, Any]]:
    """在固定任务与工具块之后追加相关指南和当前问题规划。"""
    messages = deepcopy(list(common_messages))
    if not messages:
        raise ValueError("正式问题生成的公共任务消息不能为空")
    selected_guides = render_selected_question_guides(
        guide_catalog,
        focus.attention_codes,
    )
    target = "\n\n".join(
        (
            selected_guides,
            _render_question_plan(focus),
            "下一步：核对合同页面，并调用 propose_question 提交忠实覆盖当前问题规划的自然用户问题。",
        )
    )
    messages.append(
        {
            "role": "user",
            "content": [{"type": "text", "text": target}],
        }
    )
    return messages


__all__ = [
    "QUESTION_PLAN_BEGIN",
    "QUESTION_PLAN_COMMENTS",
    "QUESTION_PLAN_END",
    "QUESTION_PROPOSAL_COMMON_PROMPT_VERSION",
    "QUESTION_PROPOSAL_COMMON_TASK",
    "QUESTION_PROPOSAL_TARGET_PROMPT_VERSION",
    "QUESTION_PROPOSAL_TOOL_PLACEMENT",
    "append_question_plan_target",
    "build_question_proposal_common_messages",
]
