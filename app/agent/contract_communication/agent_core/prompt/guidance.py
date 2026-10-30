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


def build_task_start_guidence_message():
    from ...exploration.prompt.guidance import build_task_start_guidence_message as build
    return build()


def build_progress_reminder_message():
    from ...exploration.prompt.guidance import build_progress_reminder_message as build
    return build()
