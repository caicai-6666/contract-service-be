"""主助手生成循环：每轮重读驻留上下文，串行通过 FIFO 执行工具。"""
import json
from contextlib import AsyncExitStack
from copy import deepcopy
from dataclasses import asdict
from functools import partial
from time import monotonic
from uuid import uuid4

from app.core.config import get_settings
from app.core.tool_tag import resolve_mllm_tool_tag, tool_tag_section
from app.infrastructure.mllm import MLLMClient
from app.infrastructure.vllm_tokenizer import count_chat_tokens, count_text_tokens
from app.agent.contract_extraction.tool_protocol import ToolProtocolRecovery, audited_assistant_content
from .context_budget.calculator import calculate_context_budget_from_fixed_input
from .context_rendering import render_summary_section, render_workspace_section
from .native_messages import validate_native_messages
from .page_display import PageDisplayWindow
from .prompt import build_system_guidence_message
from app.infrastructure.model_feedback import ModelFeedbackError
from .tool.executor import ToolExecutor
from .host import AgentCoreHost, AgentCoreBindings, AgentCoreResult
from .output import UserOutputReceipt
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


async def run_agent_loop(host: AgentCoreHost, bindings: AgentCoreBindings, *, client=None,
                         settings=None, audit=None, max_rounds=96, max_consecutive_errors=3,
                         management_reserve_tokens=1024, tool_call_template=None,
                         text_counter=None, chat_counter=None, fifo_counter=None,
                         fifo_compressor=None, workspace_compressor=None, additional_tools=(), page_resolver=None):
    """模式无关的单任务循环；宿主负责状态，绑定项负责提示词与交付。"""
    if any(type(n) is not int or n <= 0 for n in (max_rounds, max_consecutive_errors)):
        raise ValueError('生成和连续纠错上限必须为正整数')
    settings = settings or get_settings().mllm
    generation = settings.generation
    template = resolve_mllm_tool_tag(settings, tool_call_template)
    system = bindings.build_system_prompt(template)
    text_counter = text_counter or partial(count_text_tokens, settings=settings)
    template_kwargs = {'enable_thinking': generation.enable_thinking,
                       'tool_placement': 'before_task', 'tool_task_index': 1,
                       'preserve_thinking': True}
    chat_counter = chat_counter or partial(count_chat_tokens, settings=settings, chat_template_kwargs=template_kwargs)
    fifo_counter = fifo_counter or partial(count_fifo_task_tokens, settings=settings)
    audit = audit if audit is not None else []

    async def summary_counter(value):
        return await text_counter(render_summary_section(value)) if value is not None else 0

    async def workspace_counter(value):
        return await text_counter(render_workspace_section(value))

    async def read_context():
        return await host.read_context(include_reasoning=generation.enable_thinking)

    async def read_workspace():
        return (await read_context()).workspace

    async def commit_workspace(payload, revision):
        return await host.commit_workspace(payload, revision)

    async def commit_summary(previous, summary, scope):
        await host.commit_summary(previous, summary, scope)
        audit.append({'event': 'summary_committed', 'task_ids': list(scope.task_ids)})

    output_stream = None

    async def interrupt_output():
        if output_stream is not None:
            await output_stream.interrupt()

    last_output = None

    async def publish_output(output):
        nonlocal last_output
        # 模型不能选择会话身份或消息 ID；同一调用得到同一个程序消息标识。
        current = await read_context()
        if output.task_id != current.tasks[-1].task_id:
            raise AgentCoreLoopError('输出所属任务已变化')
        message_id = await output_stream.complete(output) if output_stream is not None else str(uuid4())
        last_output = output
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

    bindings_tools = bindings.build_output_tools(publish_output)
    registry = build_agent_tool_registry(workspace_handler=dispatch_workspace,
        output_tools=bindings_tools, additional_tools=additional_tools)
    registry.require_supported_content(('ordinary', 'foldable') if page_resolver is not None else ('ordinary',))
    pages = PageDisplayWindow(page_resolver, max_rounds=settings.page_display_rounds) if page_resolver is not None else None
    tools = registry.definitions()
    output_kinds = {entry.name: entry.output_kind for entry in bindings_tools}

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
    executor = ToolExecutor(registry.handlers(publish_progress=bindings.publish_progress))
    recovery = ToolProtocolRecovery(tool_call_template=template)
    temporary = []
    pending_feedback = []
    failures = 0
    seen_calls = set()
    request_guidance = bindings.guidance_factory() if bindings.guidance_factory is not None else None
    async with AsyncExitStack() as stack:
        stack.push_async_callback(interrupt_output)
        if pages is not None:
            stack.callback(pages.close)
        model = client if client is not None else await stack.enter_async_context(MLLMClient(settings))
        for round_number in range(1, max_rounds + 1):
            current = await read_context()
            allocation = budget(await summary_counter(current.summary))
            messages = [{'role': 'system', 'content': system}, *deepcopy(current.messages), *deepcopy(temporary)]
            guidance = request_guidance.before_request(round_number, any(
                m.get('role') in ('assistant', 'tool') for m in current.fifo[-1].messages)) if request_guidance is not None else None
            if guidance is not None:
                messages.append(guidance)
            displayed = ()
            if pages is not None:
                messages, displayed = await pages.inject(messages)
            input_tokens = await chat_counter(messages, tools)
            if input_tokens + generation.max_completion_tokens > settings.context_window_tokens:
                raise AgentCoreLoopError('完整请求与输出预留超过上下文上限，停止生成')
            started = monotonic()
            event = {'round': round_number, 'input_tokens': input_tokens,
                     'request_guidance_injected': guidance is not None}
            audit.append(event)
            output_stream = bindings.observer_factory() if bindings.observer_factory is not None else None
            try:
                response = await model.create_tool_chat_completion(messages=messages, tools=tools, tool_choice='auto',
                    max_completion_tokens=generation.max_completion_tokens, temperature=generation.temperature,
                    top_p=generation.top_p, top_k=generation.top_k, presence_penalty=generation.presence_penalty,
                    repetition_penalty=generation.repetition_penalty, seed=generation.seed, on_tool_delta=output_stream.on_delta if output_stream is not None else None, **template_kwargs)
            except BaseException as exc:
                event.update(error_type=type(exc).__name__, elapsed_seconds=monotonic() - started)
                raise
            for pending in pending_feedback:
                pending['feedback_delivered'] = True
            pending_feedback.clear()
            if pages is not None:
                pages.consume(displayed, finish_reason=response.completion.finish_reason)
            # 请求里的完整页面不进入审计；计数完成且请求返回后尽早释放本地消息引用。
            del messages
            event.update(completion=asdict(response.completion), elapsed_seconds=monotonic() - started,
                         assistant=deepcopy(response.assistant_message),
                         tool_calls=[asdict(call) for call in response.tool_calls])
            # 普通文本只留有限私有审计；原生思考仅随已接受动作进入任务轨迹。
            event['assistant']['content'] = audited_assistant_content(response.assistant_message.get('content'))
            try:
                if len(response.tool_calls) != 1:
                    raise ModelFeedbackError(f'本轮解析到{len(response.tool_calls)}个原生调用，需要恰好一个。', '选择一个当前工具，通过原生tool_calls提交，不用普通文字模拟调用。')
                if response.assistant_message.get('refusal'):
                    raise ModelFeedbackError('本轮包含拒答标记，未接受工具动作。', '能处理时提交当前工具；资料不足时用finish_task说明限制。')
                if response.completion.finish_reason not in ('stop','tool_calls'):
                    raise ModelFeedbackError('本轮响应未完整结束，不能执行截断调用。', '缩短参数，重新提交完整的单个原生调用。')
                call = response.tool_calls[0]
                assistant = {'role': 'assistant', 'content': None, 'tool_calls': [{
                    'id': call.call_id, 'type': 'function', 'function': {'name': call.name, 'arguments': call.arguments}}]}
                if generation.enable_thinking:
                    assistant.update({key: response.assistant_message[key]
                        for key in ('reasoning', 'reasoning_content')
                        if isinstance(response.assistant_message.get(key), str)
                        and response.assistant_message[key].strip()})
                validate_native_messages([assistant, {'role': 'tool', 'tool_call_id': call.call_id, 'content': '{}'}])
                if call.call_id in seen_calls:
                    raise ModelFeedbackError('调用标识重复，不能执行新动作。', '为本次新动作生成新的调用标识；不要复用旧调用ID。')
            except (ValueError, TypeError) as exc:
                await interrupt_output()
                failures += 1
                recovery.record_protocol_failure(temporary, assistant_message={},
                    tool_call_count=len(response.tool_calls), result_label='有效操作')
                # 删除空 assistant 占位，以统一模板替代固定 XML 恢复消息；不回显非法正文。
                del temporary[-2:]
                reason = exc.public_text if isinstance(exc, ModelFeedbackError) else '原生调用身份或arguments不符合协议；arguments必须为合法JSON对象，禁止重复键及非有限数值。本次未执行。'
                guidance = build_system_guidence_message(kind='output_error', reason=reason,
                    required_action='按上述原因修正，提交完整单个原生调用。' + tool_tag_section(template))
                temporary.append(guidance)
                event.update(feedback=guidance, feedback_delivered=False)
                pending_feedback.append(event)
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
            kind = output_kinds.get(call.name)
            record('feedback' if kind == 'intermediate' else 'result' if kind == 'final' else 'tool',
                   call.name, tool=call.name, input=call.arguments, output=event['result'],
                   status='completed' if event['accepted'] else 'error')
            if result.execution_status != 'succeeded':
                await interrupt_output()
            if result.execution_status == 'succeeded':
                recovery.accept_correction(temporary)
                failures = 0
                await host.record_exchange(assistant, FIFOExecutionResult(status='succeeded',
                        tool_result=result.tool_result, content=result.content, system_guidence=result.system_guidence))
                if result.status == 'success' and result.can_continue:
                    await executor.commit(operation)
                if pages is not None and result.can_continue:
                    pages.register(call.call_id, FIFOExecutionResult(status='succeeded',
                        tool_result=result.tool_result, content=result.content))
                kind = output_kinds.get(operation.name)
                if kind is not None and request_guidance is not None:
                    request_guidance.after_output(kind)
                if kind == 'final' and result.tool_result.get('finish_requested') is True:
                    # 已接受最终动作后，即使容量不足也必须收束，禁止重复交付。
                    if last_output is None or last_output.call_id != operation.call_id:
                        raise AgentCoreLoopError('最终输出缺少已验收正文')
                    completed = AgentCoreResult(task_id=operation.task_id, content=last_output.content, rounds=round_number)
                    await host.complete(completed)
                    return completed
                if not result.can_continue:
                    raise AgentCoreLoopError(result.error_feedback or '执行成功但无法继续生成')
            elif result.execution_status == 'failed' and result.result_type == 'tool_error':
                # 管理失败不允许正常继续；仅对明确未生效的工具开放有限纠错。
                # 下一轮仍先检查完整请求容量，unknown/容量失败不走此路径。
                failures += 1
                # 无效参数、未知属性和附带正文只留审计，恢复消息不重放失败调用。
                recovery.record_tool_failure(temporary, assistant_message={'role':'assistant','content':''},
                    tool_message=build_system_guidence_message(kind='invalid_action',
                        reason=json.dumps(result.tool_result,ensure_ascii=False,allow_nan=False),
                        required_action=result.tool_result.get('required_action') or '依据工具诊断修正后再提交；不要把本次失败当作已完成。'))
                temporary.extend(g.message for g in result.system_guidence)
                # 定向原因来自工具原始回执；外层只传递，不追加泛化文案盖过修复方向。
                event.update(feedback_delivered=False)
                pending_feedback.append(event)
                if failures >= max_consecutive_errors:
                    raise AgentCoreLoopError('连续工具校验失败达到上限')
            else:
                raise AgentCoreLoopError(result.error_feedback or '工具状态不确定或容量不足，停止执行')
        raise AgentCoreLoopError('主助手生成轮数达到上限')


async def run_agent_core(service, conversation_id, turn_id, owner, **kwargs):
    """既有会话调用的兼容入口；正式装配使用 exploration.run_exploration。"""
    from ..exploration import run_exploration
    return await run_exploration(service, conversation_id, turn_id, owner, **kwargs)
