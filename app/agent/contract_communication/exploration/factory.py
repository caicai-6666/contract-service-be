"""将已有 Communication 会话接到通用核心，保持 SSE 与历史协议。"""
from app.schema.communication import TurnStatusData
from ..agent_core.host import AgentCoreBindings
from ..agent_core.runtime import run_agent_loop
from .prompt import build_system_prompt
from .prompt.guidance import build_task_start_guidence_message
from .progress_reminder import ProgressReminder
from .output_stream import InteractionOutputStream
from .tool.interaction import build_interaction_registrations


class ExplorationHost:
    """会话所有权与原子写入继续由既有服务验证。"""
    def __init__(self, service, conversation_id, turn_id, owner):
        self.service, self.conversation_id, self.turn_id, self.owner = service, conversation_id, turn_id, owner

    async def read_context(self, *, include_reasoning):
        return await self.service.get_agent_core_context(self.conversation_id, self.turn_id,
            owner=self.owner, include_reasoning=include_reasoning)

    async def commit_workspace(self, payload, revision):
        return await self.service.commit_agent_core_workspace(self.conversation_id, self.turn_id,
            owner=self.owner, payload=payload, expected_revision=revision)

    async def commit_summary(self, previous, summary, scope):
        return await self.service.commit_agent_core_summary(self.conversation_id, self.turn_id,
            owner=self.owner, expected_summary=previous, summary=summary, scope=scope)

    async def record_exchange(self, assistant, result):
        return await self.service.record_agent_core_exchange(self.conversation_id, self.turn_id,
            owner=self.owner, assistant_message=assistant, result=result)

    async def complete(self, result):
        await self.service.publish(self.conversation_id, self.turn_id, owner=self.owner,
            data=TurnStatusData(status='completed'))


class ExplorationGuidance:
    def __init__(self, interval):
        self.reminder = ProgressReminder(interval)

    def before_request(self, round_number, has_exchanges):
        if round_number == 1 and not has_exchanges:
            return build_task_start_guidence_message()
        return self.reminder.message()

    def after_output(self, kind):
        if kind == 'intermediate':
            self.reminder.acknowledge()


async def run_exploration(service, conversation_id, turn_id, owner, *, context=None, settings=None, audit=None, **kwargs):
    from app.core.config import get_settings
    settings = settings or get_settings().mllm
    host = ExplorationHost(service, conversation_id, turn_id, owner)
    events = audit if audit is not None else []
    service._agent_core_audits[(conversation_id, turn_id)] = events

    async def publish_progress(progress):
        await service.publish_tool_progress(conversation_id, turn_id, owner=owner, progress=progress)

    bindings = AgentCoreBindings(build_system_prompt=build_system_prompt,
        build_output_tools=build_interaction_registrations,
        observer_factory=lambda: InteractionOutputStream(service, conversation_id, turn_id, owner),
        guidance_factory=lambda: ExplorationGuidance(settings.progress_reminder_interval_seconds),
        publish_progress=publish_progress)
    return await run_agent_loop(host, bindings, settings=settings, audit=events, **kwargs)
