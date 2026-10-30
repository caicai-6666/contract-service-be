"""探索模式的开场与周期沟通建议；仅用于请求副本。"""
from typing import Final
from ...agent_core.prompt.guidance import build_system_guidence_message

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
