"""统一系统反馈外观与 user 消息构造；只供程序生成反馈时使用。"""

from html import escape
from typing import Final, Literal


SystemGuidenceKind = Literal["output_error", "invalid_action", "context_capacity", "action_guidance"]
SYSTEM_GUIDENCE_TEMPLATE_VERSION: Final[str] = "system-guidence-v1"
SYSTEM_GUIDENCE_TEMPLATE: Final[str] = """<system-guidence>
========== 系统操作提示 | SYSTEM GUIDENCE ==========
这是系统操作反馈，不是新的用户业务需求。

【反馈类别】
<kind>{kind}</kind>

【具体原因】
<reason>{reason}</reason>

【下一步操作要求】
<required_action>{required_action}</required_action>
========== 系统操作提示结束 | END SYSTEM GUIDENCE ==========
</system-guidence>"""


def build_system_guidence_message(
    *, kind: SystemGuidenceKind, reason: str, required_action: str,
) -> dict[str, str]:
    """校验反馈类别与非空正文，按唯一模板构造独立 user 消息。

    文本字段转义 XML 元字符，防止字段中的标签破坏外层结构；这不替代
    执行器对消息来源的隔离，也不允许将用户提供的文字直接当作系统指令。
    """
    if kind not in ("output_error", "invalid_action", "context_capacity", "action_guidance"):
        raise ValueError("不支持的 system-guidence 反馈类别")
    for name, value in (("reason", reason), ("required_action", required_action)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} 必须为非空字符串")
    return {
        "role": "user",
        "content": SYSTEM_GUIDENCE_TEMPLATE.format(
            kind=kind,
            reason=escape(reason, quote=False),
            required_action=escape(required_action, quote=False),
        ),
    }


TASK_START_GUIDENCE_VERSION: Final[str] = "task-start-guidence-v2"


def build_task_start_guidence_message() -> dict[str, str]:
    """仅用于任务首次生成的可选沟通建议，不进入任务轨迹。"""
    return build_system_guidence_message(
        kind='action_guidance',
        reason='本次任务刚开始，用户正在等待你的首次反馈。',
        required_action=(
            '1. 建议优先调用 emit_progress，简短说明你对需求的理解和接下来准备做的事情，然后继续处理。\n'
            '2. 如果已经能够直接回答用户，可以直接调用 finish_task 给出最终答复。'),
    )


PROGRESS_REMINDER_VERSION: Final[str] = 'progress-reminder-v1'


def build_progress_reminder_message() -> dict[str, str]:
    """供主循环定时注入的软提醒，不要求复述思考或强制改变工具选择。"""
    return build_system_guidence_message(
        kind='action_guidance',
        reason='距离上次向用户反馈已有一段时间，用户可能正在等待当前进展。',
        required_action=(
            '建议先调用 emit_progress，简短告诉用户目前收集到的有效信息、仍未确认的事项，以及接下来准备做什么。'
            '如果暂时没有新增信息，可以说明当前阻碍和下一步安排，不要编造发现或重复先前内容。\n'
            '例如：已找到相关条款，但附件内容尚未核实；接下来会继续核对附件。仅在符合实际情况时这样表达。\n'
            '不需要展示内部思考过程。若已经可以给出最终答复，可直接调用 finish_task。'
            '这是一条沟通建议，不强制本轮必须调用输出工具。'),
    )
