"""工具型模型节点共享的 non-strict auto 协议与纠错短期记忆。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from app.core.tool_tag import get_mllm_tool_tag, tool_tag_section

TOOL_CHOICE_AUTO: Final = "auto"
MAXIMUM_PROTOCOL_RECOVERIES: Final = 2
MAXIMUM_AUDITED_ASSISTANT_CONTENT: Final = 1_000


def build_protocol_recovery_message(
    *,
    tool_call_count: int,
    result_label: str,
    tool_call_template: str | None = None,
) -> dict[str, str]:
    """使用与任务一致的工具格式纠错，不回显错误输出。"""
    template = tool_call_template if tool_call_template is not None else get_mllm_tool_tag()
    return {
        "role": "user",
        "content": (
            f"上一轮未生成合法工具调用：服务端只解析到 {tool_call_count} 个工具调用，"
            f"该响应不能作为{result_label}。"
            "不要输出“工具名: JSON”、参数说明或普通文本来模拟调用。"
            "本轮必须且只能调用一个当前提供的工具。"
            f"{tool_tag_section(template)}"
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
    """维护协议与工具校验失败共用的临时短期记忆范围。

    失败对话只为下一轮纠错服务。某个工具动作通过全部校验后，调用方
    必须先清除这段临时轨迹，再决定是否把正确动作写入长期会话。
    审计记录由各节点独立维护，不受消息清理影响。
    """

    maximum_attempts: int = MAXIMUM_PROTOCOL_RECOVERIES
    attempts: int = 0
    memory_start: int | None = None
    tool_call_template: str | None = None

    def _start_memory(self, messages: list[dict[str, Any]]) -> None:
        """只在连续失败链的第一轮记录清理边界。"""
        if self.memory_start is None:
            self.memory_start = len(messages)

    def record_protocol_failure(
        self,
        messages: list[dict[str, Any]],
        *,
        assistant_message: dict[str, Any],
        tool_call_count: int,
        result_label: str,
    ) -> bool:
        """追加无合法单工具响应；返回是否超过连续协议恢复上限。"""
        self._start_memory(messages)
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
                tool_call_template=self.tool_call_template,
            )
        )
        self.attempts += 1
        return self.attempts > self.maximum_attempts

    def record_failure(
        self,
        messages: list[dict[str, Any]],
        *,
        assistant_message: dict[str, Any],
        tool_call_count: int,
        result_label: str,
    ) -> bool:
        """兼容原调用名；新代码应使用 record_protocol_failure。"""
        return self.record_protocol_failure(
            messages,
            assistant_message=assistant_message,
            tool_call_count=tool_call_count,
            result_label=result_label,
        )

    def accept_protocol(self) -> None:
        """已形成单工具调用，仅重置协议失败计数，不清理纠错轨迹。"""
        self.attempts = 0

    def record_tool_failure(
        self,
        messages: list[dict[str, Any]],
        *,
        assistant_message: dict[str, Any],
        tool_message: dict[str, Any],
    ) -> None:
        """追加工具解析、Schema、状态或业务校验失败对话。"""
        self._start_memory(messages)
        self.accept_protocol()
        messages.append(assistant_message)
        messages.append(tool_message)

    def accept_correction(self, messages: list[dict[str, Any]]) -> None:
        """动作通过全部校验后删除失败轨迹，并重置恢复状态。"""
        if self.memory_start is not None:
            del messages[self.memory_start :]
        self.attempts = 0
        self.memory_start = None


__all__ = [
    "MAXIMUM_AUDITED_ASSISTANT_CONTENT",
    "MAXIMUM_PROTOCOL_RECOVERIES",
    "TOOL_CHOICE_AUTO",
    "ToolProtocolRecovery",
    "audited_assistant_content",
    "build_protocol_recovery_message",
]
