"""A2A 内部任务入口；未来传输与持久化由受信宿主适配。"""
import asyncio
from ..agent_core.host import AgentCoreBindings
from ..agent_core.runtime import run_agent_loop
from .prompt import build_system_prompt
from .tool.finish_task import build_output_tools
from .model import A2ATaskRequest, A2ATaskResult
from .task import InMemoryA2ATask



async def run_a2a_task(request, *, host=None, **kwargs):
    tools = tuple(kwargs.get('additional_tools', ()))
    for tool in tools:
        if tool.name in {'view_session_file', 'search_memory', 'view_memory_query'}:
            raise ValueError('A2A不提供附件或记忆检索工具')
        if 'contract' in tool.name and tool.access_policy is None:
            raise ValueError('A2A合同工具必须绑定服务端访问边界')
    policies = {id(t.access_policy) for t in tools if t.access_policy is not None}
    if len(policies) > 1:
        raise ValueError('同一任务不能混用不同访问边界')
    request = A2ATaskRequest.model_validate(request)
    host = host if host is not None else InMemoryA2ATask(request)
    if host.request != request:
        raise ValueError('宿主绑定任务与本次请求不一致')
    host.start()
    try:
        result = await run_agent_loop(host, AgentCoreBindings(build_system_prompt=build_system_prompt,
            build_output_tools=build_output_tools), **kwargs)
        return A2ATaskResult(task_id=result.task_id, content=result.content, rounds=result.rounds)
    except asyncio.CancelledError:
        host.abort('cancelled')
        raise
    except BaseException:
        host.abort('failed')
        raise
