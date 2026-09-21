"""主助手生成循环：每轮重读驻留上下文，串行通过 FIFO 执行工具。"""
import json
from contextlib import AsyncExitStack
from copy import deepcopy
from dataclasses import asdict
from functools import partial
from time import monotonic

from app.core.config import get_settings
from app.core.tool_tag import get_mllm_tool_tag
from app.infrastructure.mllm import MLLMClient
from app.infrastructure.vllm_tokenizer import count_chat_tokens, count_text_tokens
from app.schema.communication import TurnStatusData
from app.agent.contract_extraction.tool_protocol import ToolProtocolRecovery, audited_assistant_content
from .context_budget.calculator import calculate_context_budget_from_fixed_input
from .context_rendering import render_summary_section, render_workspace_section
from .native_messages import validate_native_messages
from .page_display import PageDisplayWindow
from .progress_reminder import ProgressReminder
from .prompt import (AGENT_CORE_ROLE_PROMPT, AGENT_CORE_WORKSPACE_PROMPT, AGENT_CORE_FIFO_PROMPT,
                     build_agent_core_interaction_prompt, build_system_guidence_message)
from .prompt.guidance import build_task_start_guidence_message
from .tool.executor import ToolExecutor
from .output_stream import InteractionOutputStream
from .tool.interaction import UserOutputReceipt
from .tool.registry import build_agent_tool_registry
from .tool.workspace_executor import WorkspaceToolHandler
from .subgraph.workspace_management.workflow import build_workspace_management_subgraph
from .subgraph.workspace_management.compression.runtime import run_compression_tool_loop
from .subgraph.fifo_management.workflow import build_fifo_management_subgraph
from .subgraph.fifo_management.schema import FIFOOperation, FIFOExecutionResult, FIFOManagementResult
from .subgraph.fifo_management.token_count import count_fifo_task_tokens
from .subgraph.fifo_management.compression import FIFOCompressionResult
from .subgraph.fifo_management.fifo_summary.workflow import build_fifo_summary_subgraph
from .subgraph.fifo_management.fifo_summary.planner import run_topic_planner
from .subgraph.fifo_management.fifo_summary.generator import run_topic_generator


class AgentCoreLoopError(RuntimeError):
    """停止本轮，交由工作流发布失败终态；已确认的副作用不得回滚或重放。"""


async def run_agent_core(service, conversation_id, turn_id, owner, *, context=None, client=None,
                         settings=None, audit=None, max_rounds=96, max_consecutive_errors=3,
                         management_reserve_tokens=1024, tool_call_template=None,
                         text_counter=None, chat_counter=None, fifo_counter=None,
                         fifo_compressor=None, workspace_compressor=None, additional_tools=(), page_resolver=None):
    """可注入客户端/计数器进行离线验证；生产默认使用同一 vLLM 配置。

    context 是门禁后的首次快照；每轮均重新读取，不能把初始快照缓存为第二份权威历史。
    私有审计归属本轮服务实例，不进入历史渲染、SSE 或摘要输入。
    additional_tools 提供完整注册项，统一参与工具定义、固定预算及 FIFO 执行。
    page_resolver 为异步页面引用解析器；提供后启用可配置轮数的展示，资源生命周期由解析器宿主管理。
    """
    if any(type(n) is not int or n <= 0 for n in (max_rounds, max_consecutive_errors)):
        raise ValueError('生成和连续纠错上限必须为正整数')
    settings = settings or get_settings().mllm
    generation = settings.generation
    template = tool_call_template if tool_call_template is not None else get_mllm_tool_tag()
    system = '\n\n'.join((AGENT_CORE_ROLE_PROMPT,
        build_agent_core_interaction_prompt(tool_call_template=template),
        AGENT_CORE_WORKSPACE_PROMPT, AGENT_CORE_FIFO_PROMPT))
    text_counter = text_counter or partial(count_text_tokens, settings=settings)
    template_kwargs = {'enable_thinking': generation.enable_thinking,
                       'tool_placement': 'before_task', 'tool_task_index': 1}
    chat_counter = chat_counter or partial(count_chat_tokens, settings=settings, chat_template_kwargs=template_kwargs)
    fifo_counter = fifo_counter or partial(count_fifo_task_tokens, settings=settings)
    audit = audit if audit is not None else []
    service._agent_core_audits[(conversation_id, turn_id)] = audit

    async def summary_counter(value):
        return await text_counter(render_summary_section(value)) if value is not None else 0

    async def workspace_counter(value):
        return await text_counter(render_workspace_section(value))

    async def read_context():
        return await service.get_agent_core_context(conversation_id, turn_id, owner=owner, include_reasoning=generation.enable_thinking,
                reasoning_limits=(settings.reasoning_window_rounds, settings.reasoning_window_max_tokens))

    async def read_workspace():
        return (await read_context()).workspace

    async def commit_workspace(payload, revision):
        return await service.commit_agent_core_workspace(conversation_id, turn_id, owner=owner,
                                                         payload=payload, expected_revision=revision)

    async def commit_summary(previous, summary, scope):
        await service.commit_agent_core_summary(conversation_id, turn_id, owner=owner,
            expected_summary=previous, summary=summary, scope=scope)
        audit.append({'event': 'summary_committed', 'task_ids': list(scope.task_ids)})

    async def publish_output(output):
        # 模型不能选择会话身份或消息 ID；同一调用得到同一个程序消息标识。
        current = await read_context()
        if output.task_id != current.tasks[-1].task_id:
            raise AgentCoreLoopError('输出所属任务已变化')
        message_id = await output_stream.complete(output)
        return UserOutputReceipt(message_id=message_id)

    async def compress_summary(request):
        child_audit = []
        audit.append({'event': 'summary_generation', 'events': child_audit})
        async def planner(value):
            return await run_topic_planner(value, settings=settings, audit=child_audit, tool_call_template=template)
        async def generator(value):
            events = []
            child_audit.append({'topic_id': value.topic.topic_id, 'events': events})
            return await run_topic_generator(value, settings=settings, audit=events)
        result = (await build_fifo_summary_subgraph(planner=planner, generator=generator).ainvoke({'request': request}))['result']
        if result.status != 'success':
            raise AgentCoreLoopError('自动摘要未通过验收')
        return FIFOCompressionResult(summary=result.summary)

    async def compress_workspace(request):
        events = []
        audit.append({'event': 'workspace_compression', 'events': events})
        return await run_compression_tool_loop(request, settings=settings, counter=workspace_counter,
                                               audit=events, tool_call_template=template)

    async def dispatch_workspace(operation):
        # 注册先于预算计算，实际调用发生在管理图装配完成之后。
        return await workspace_handler(operation)

    registry = build_agent_tool_registry(workspace_handler=dispatch_workspace,
        publish_output=publish_output, additional_tools=additional_tools)
    registry.require_supported_content(('ordinary', 'foldable') if page_resolver is not None else ('ordinary',))
    pages = PageDisplayWindow(page_resolver, max_rounds=settings.page_display_rounds) if page_resolver is not None else None
    tools = registry.definitions()

    # 同一套模板合计固定输入；动态区为空，仅保留实际消息外壳和生成前缀。
    fixed = await chat_counter([{'role': 'system', 'content': system}, {'role': 'user', 'content': ''}], tools)
    def budget(summary_tokens):
        return calculate_context_budget_from_fixed_input(context_window_tokens=settings.context_window_tokens,
            fixed_input_tokens=fixed, output_reserve_tokens=generation.max_completion_tokens,
            management_reserve_tokens=management_reserve_tokens, summary_tokens=summary_tokens)
    initial_budget = budget(0)
    workspace_graph = build_workspace_management_subgraph(token_budget=initial_budget.workspace_budget_tokens,
        counter=workspace_counter, compressor=workspace_compressor or compress_workspace)
    workspace_handler = WorkspaceToolHandler(graph=workspace_graph, read_workspace=read_workspace, commit=commit_workspace)
    async def publish_progress(progress):
        await service.publish_tool_progress(conversation_id, turn_id, owner=owner, progress=progress)

    executor = ToolExecutor(registry.handlers(publish_progress=publish_progress))
    recovery = ToolProtocolRecovery()
    temporary = []
    failures = 0
    seen_calls = set()
    progress_reminder = ProgressReminder(settings.progress_reminder_interval_seconds, clock=monotonic)
    async with AsyncExitStack() as stack:
        if pages is not None:
            stack.callback(pages.close)
        model = client if client is not None else await stack.enter_async_context(MLLMClient(settings))
        for round_number in range(1, max_rounds + 1):
            current = await read_context()
            allocation = budget(await summary_counter(current.summary))
            messages = [{'role': 'system', 'content': system}, *deepcopy(current.messages), *deepcopy(temporary)]
            # 只加在第一次请求的临时副本，纳入本次计数；不写入驻留轨迹或纠错记忆。
            # 已有执行轨迹的任务恢复时不重复提供开场建议。
            opening_guidance = round_number == 1 and not any(m.get('role') in ('assistant', 'tool') for m in current.fifo[-1].messages)
            if opening_guidance:
                messages.append(build_task_start_guidence_message())
            displayed = ()
            if pages is not None:
                messages, displayed = await pages.inject(messages)
            # 页面、纠错等上下文装配后再追加，始终位于尾部；不写入 temporary。
            # 开场建议与定时提醒不在同一请求重复出现。
            reminder_message = progress_reminder.message() if not opening_guidance else None
            if reminder_message is not None:
                messages.append(reminder_message)
            input_tokens = await chat_counter(messages, tools)
            if input_tokens + generation.max_completion_tokens > settings.context_window_tokens:
                raise AgentCoreLoopError('完整请求与输出预留超过上下文上限，停止生成')
            started = monotonic()
            event = {'round': round_number, 'input_tokens': input_tokens,
                     'progress_reminder_injected': reminder_message is not None}
            audit.append(event)
            output_stream = InteractionOutputStream(service, conversation_id, turn_id, owner)
            async def on_tool_delta(message):
                await output_stream.on_delta(message)
            try:
                response = await model.create_tool_chat_completion(messages=messages, tools=tools, tool_choice='auto',
                    max_completion_tokens=generation.max_completion_tokens, temperature=generation.temperature,
                    top_p=generation.top_p, top_k=generation.top_k, presence_penalty=generation.presence_penalty,
                    repetition_penalty=generation.repetition_penalty, seed=generation.seed, on_tool_delta=on_tool_delta, **template_kwargs)
            except BaseException as exc:
                event.update(error_type=type(exc).__name__, elapsed_seconds=monotonic() - started)
                raise
            if pages is not None:
                pages.consume(displayed, finish_reason=response.completion.finish_reason)
            # 请求里的完整页面不进入审计；计数完成且请求返回后尽早释放本地消息引用。
            del messages
            event.update(completion=asdict(response.completion), elapsed_seconds=monotonic() - started,
                         assistant=deepcopy(response.assistant_message),
                         tool_calls=[asdict(call) for call in response.tool_calls])
            # 普通文本只留有限私有审计；已接受动作的原生推理由独立窗口按位置回注。
            event['assistant']['content'] = audited_assistant_content(response.assistant_message.get('content'))
            try:
                if (len(response.tool_calls) != 1 or response.assistant_message.get('content') not in (None, '')
                        or response.assistant_message.get('refusal')
                        or response.completion.finish_reason not in ('stop', 'tool_calls')):
                    raise ValueError('需要完整的单个函数调用')
                call = response.tool_calls[0]
                assistant = {'role': 'assistant', 'content': None, 'tool_calls': [{
                    'id': call.call_id, 'type': 'function', 'function': {'name': call.name, 'arguments': call.arguments}}]}
                validate_native_messages([assistant, {'role': 'tool', 'tool_call_id': call.call_id, 'content': '{}'}])
                if call.call_id in seen_calls:
                    raise ValueError('调用标识重复')
            except (ValueError, TypeError):
                await output_stream.interrupt()
                failures += 1
                recovery.record_protocol_failure(temporary, assistant_message={},
                    tool_call_count=len(response.tool_calls), result_label='有效操作')
                # 删除空 assistant 占位，以统一模板替代固定 XML 恢复消息；不回显非法正文。
                del temporary[-2:]
                temporary.append(build_system_guidence_message(kind='output_error',
                    reason='响应必须是完整的单个函数调用，参数为合法 JSON，调用标识不可重复。',
                    required_action='请提交一个当前工具调用；调用格式如下：\n' + template))
                event['accepted'] = False
                if failures >= max_consecutive_errors:
                    raise AgentCoreLoopError('连续输出校验失败达到上限')
                continue
            seen_calls.add(call.call_id)
            operation = FIFOOperation(call_id=call.call_id, task_id=current.tasks[-1].task_id,
                                      name=call.name, arguments=call.arguments)
            fifo = [task.model_copy(deep=True) for task in current.fifo]
            fifo[-1].messages.append(assistant)
            graph = build_fifo_management_subgraph(token_budget=allocation.fifo_budget_tokens,
                counter=fifo_counter, executor=executor, compressor=fifo_compressor or compress_summary,
                summary_counter=summary_counter, summary_commit=commit_summary, supports_foldable=pages is not None)
            result = FIFOManagementResult.model_validate((await graph.ainvoke({'request': {
                'fifo': fifo, 'summary': current.summary, 'operation': operation}}))['result'])
            event['result'] = result.model_dump(mode='json', exclude={'fifo', 'summary'})
            event['accepted'] = result.execution_status == 'succeeded'
            from app.infrastructure.development_trace import record
            record('feedback' if call.name == 'emit_progress' else 'result' if call.name == 'finish_task' else 'tool',
                   call.name, tool=call.name, input=call.arguments, output=event['result'],
                   status='completed' if event['accepted'] else 'error')
            if result.execution_status != 'succeeded':
                await output_stream.interrupt()
            if result.execution_status == 'succeeded':
                if operation.name == 'emit_progress':
                    progress_reminder.acknowledge()
                recovery.accept_correction(temporary)
                failures = 0
                await service.record_agent_core_exchange(conversation_id, turn_id, owner=owner,
                    assistant_message=assistant, result=FIFOExecutionResult(status='succeeded',
                        tool_result=result.tool_result, content=result.content, system_guidence=result.system_guidence))
                if pages is not None and result.can_continue:
                    pages.register(call.call_id, FIFOExecutionResult(status='succeeded',
                        tool_result=result.tool_result, content=result.content))
                if generation.enable_thinking:
                    fields = {key: response.assistant_message[key] for key in ('reasoning', 'reasoning_content')
                              if isinstance(response.assistant_message.get(key), str) and response.assistant_message[key].strip()}
                    if fields:
                        tokens = sum([await text_counter(value) for value in fields.values()])
                        await service.record_agent_reasoning(conversation_id, turn_id, owner=owner,
                            call_id=call.call_id, fields=fields, tokens=tokens,
                            max_rounds=settings.reasoning_window_rounds, max_tokens=settings.reasoning_window_max_tokens)
                if operation.name == 'finish_task' and result.tool_result.get('finish_requested') is True:
                    # 最终输出成功后即使容量处理失败也应封闭本轮，不能重发最终正文。
                    await service.publish(conversation_id, turn_id, owner=owner, data=TurnStatusData(status='completed'))
                    return
                if not result.can_continue:
                    raise AgentCoreLoopError(result.error_feedback or '执行成功但无法继续生成')
            elif result.execution_status == 'failed' and result.result_type == 'tool_error':
                # 管理失败不允许正常继续；仅对明确未生效的工具开放有限纠错。
                # 下一轮仍先检查完整请求容量，unknown/容量失败不走此路径。
                failures += 1
                recovery.record_tool_failure(temporary, assistant_message=assistant, tool_message={
                    'role': 'tool', 'tool_call_id': call.call_id,
                    'content': json.dumps(result.tool_result, ensure_ascii=False, allow_nan=False)})
                temporary.extend(g.message for g in result.system_guidence)
                temporary.append(build_system_guidence_message(kind='invalid_action',
                    reason='工具操作未被接受，请依据工具反馈修正。', required_action='修正参数或前置条件，提交一个合法调用。'))
                if failures >= max_consecutive_errors:
                    raise AgentCoreLoopError('连续工具校验失败达到上限')
            else:
                raise AgentCoreLoopError(result.error_feedback or '工具状态不确定或容量不足，停止执行')
        raise AgentCoreLoopError('主助手生成轮数达到上限')
