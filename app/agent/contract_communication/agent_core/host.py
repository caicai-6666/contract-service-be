"""执行核心的宿主与模式装配契约；不依赖 HTTP、SSE 或会话服务。"""
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from .context import AgentCoreContext
from .output import OutputObserver, UserOutputPublisher
from .tool.progress import ToolProgressPublisher


@dataclass(frozen=True)
class AgentCoreResult:
    task_id: str
    content: str
    rounds: int


class AgentCoreHost(Protocol):
    async def read_context(self, *, include_reasoning: bool) -> AgentCoreContext: ...
    async def commit_workspace(self, payload: dict, revision: int): ...
    async def commit_summary(self, previous, summary, scope): ...
    async def record_exchange(self, assistant: dict, result): ...
    async def complete(self, result: AgentCoreResult): ...


class RequestGuidance(Protocol):
    def before_request(self, round_number: int, has_exchanges: bool) -> dict | None: ...
    def after_output(self, kind: str): ...


@dataclass(frozen=True)
class AgentCoreBindings:
    """在运行前确定模式行为；每次运行创建独立观察器和提醒实例。"""
    build_system_prompt: Callable[[str], str]
    build_output_tools: Callable[[UserOutputPublisher], list]
    observer_factory: Callable[[], OutputObserver] | None = None
    guidance_factory: Callable[[], RequestGuidance] | None = None
    publish_progress: ToolProgressPublisher | None = None
