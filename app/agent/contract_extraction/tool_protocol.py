"""工具型模型节点共享的 non-strict auto 协议与短期记忆恢复。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final


TOOL_CHOICE_AUTO: Final = "auto"
MAXIMUM_PROTOCOL_RECOVERIES: Final = 2
MAXIMUM_AUDITED_ASSISTANT_CONTENT: Final = 1_000

TOOL_CALL_XML_INSTRUCTION: Final = """合法工具调用必须使用以下 XML 结构，不得使用“工具名: JSON”、代码块或普通文本模拟调用：
<tool_call>
<function=工具名称>
<parameter=参数名称>
参数值
</parameter>
</function>
</tool_call>
请替换为实际工具名称和全部必填参数；调用结束后不得追加任何文本。"""


def build_protocol_recovery_message(
    *,
    tool_call_count: int,
    result_label: str,
) -> dict[str, str]:
    """构造不回显错误输出、明确真实 XML 协议的统一恢复反馈。"""
    return {
        "role": "user",
        "content": (
            f"上一轮未生成合法工具调用：服务端只解析到 {tool_call_count} 个工具调用，"
            f"该响应不能作为{result_label}。"
            "不要输出“工具名: JSON”、参数说明或普通文本来模拟调用。"
            "本轮必须且只能调用一个当前提供的工具，并且必须按以下 XML 模板输出：\n"
            "<tool_call>\n"
            "<function=工具名称>\n"
            "<parameter=参数名称>\n"
            "参数值\n"
            "</parameter>\n"
            "</function>\n"
            "</tool_call>\n"
            "请使用实际工具名称和全部必填参数替换模板占位内容；"
            "函数调用之后不要追加任何文本。"
        ),
    }


def audited_assistant_content(value: object) -> str | None:
    """保留有限普通文本用于私有审计，避免运行状态无界增长。"""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized[:MAXIMUM_AUDITED_ASSISTANT_CONTENT] or None


@dataclass(slots=True)
class ToolProtocolRecovery:
    """维护连续协议失败次数及其临时短期记忆范围。"""

    maximum_attempts: int = MAXIMUM_PROTOCOL_RECOVERIES
    attempts: int = 0
    memory_start: int | None = None

    def record_failure(
        self,
        messages: list[dict[str, Any]],
        *,
        assistant_message: dict[str, Any],
        tool_call_count: int,
        result_label: str,
    ) -> bool:
        """追加临时失败轨迹；返回是否已超过恢复上限。"""
        if self.memory_start is None:
            self.memory_start = len(messages)
        messages.append(
            {
                "role": "assistant",
                "content": assistant_message.get("content") or "",
            }
        )
        messages.append(
            build_protocol_recovery_message(
                tool_call_count=tool_call_count,
                result_label=result_label,
            )
        )
        self.attempts += 1
        return self.attempts > self.maximum_attempts

    def accept_correction(self, messages: list[dict[str, Any]]) -> None:
        """成功纠正后删除临时轨迹，并重置连续失败预算。"""
        if self.memory_start is not None:
            del messages[self.memory_start :]
        self.attempts = 0
        self.memory_start = None


__all__ = [
    "MAXIMUM_AUDITED_ASSISTANT_CONTENT",
    "MAXIMUM_PROTOCOL_RECOVERIES",
    "TOOL_CALL_XML_INSTRUCTION",
    "TOOL_CHOICE_AUTO",
    "ToolProtocolRecovery",
    "audited_assistant_content",
    "build_protocol_recovery_message",
]
