"""批量筛选、单任务整理向量化与待入库汇总；不执行落盘。"""

from copy import deepcopy
from dataclasses import asdict
import json
from time import perf_counter

from pydantic import ValidationError

from app.agent.conversation_memory.prompt import (
    build_memory_planning_messages, build_system_guidence_message,
    MEMORY_PLANNING_PROMPT_VERSION, MEMORY_PLANNING_TOOL_PLACEMENT,
    build_memory_summarizing_messages, MEMORY_SUMMARIZING_PROMPT_VERSION,
    MEMORY_SUMMARIZING_TOOL_PLACEMENT,
)
from app.agent.conversation_memory.tool import (
    MEMORY_PLANNING_TOOLS, MEMORY_PLANNING_TOOL_VERSION, MemoryPlanningTools,
    MemoryFlowViolation, MemorySummarizingTools, MEMORY_SUMMARIZING_TOOLS,
    MEMORY_SUMMARIZING_TOOL_VERSION, SelectedMemoryTask,
)
from app.core.config import get_settings
from app.core.tool_tag import get_mllm_tool_tag
from app.infrastructure.mllm import MLLMClient, MLLMRequestError, MLLMUnavailableError
from app.agent.conversation_memory.embedding import embed_memory_text

from app.agent.conversation_memory.state import (
    ConversationMemoryState, MemoryGenerationInput, MemoryGenerationOutput,
    MemoryTaskInput, MemoryTaskResult, MemoryWorkerState, MemoryPendingRecord,
)


_MAXIMUM_ROUNDS = 128
_MAXIMUM_CONSECUTIVE_FAILURES = 3


def _tool_error_message(error: ValueError, name: str, tools=MEMORY_PLANNING_TOOLS) -> str:
    """从实际工具定义说明允许字段，不回显参数值、未知字段名或异常上下文。"""
    definition = next((t['function'] for t in tools
                       if t['function']['name'] == name), None)
    if definition is None:
        return '未知工具，请使用 ' + '、'.join(t['function']['name'] for t in tools) + '。'
    properties = definition['parameters']['properties']
    prefix = f'{name} 调用未接受。只允许参数：' + '、'.join(properties) + '。'
    if isinstance(error, ValidationError):
        items = []
        for item in error.errors(include_input=False, include_url=False):
            field = next((p for p in item['loc'] if p in properties), None)
            kind = item['type']
            if kind == 'extra_forbidden':
                message = '存在多余参数，请删除全部未定义参数。'
            elif kind == 'missing':
                message = f'{field or "arguments"}：缺少必填参数，请补齐。'
            elif field is not None:
                schema = properties[field]
                # nullable正文的约束位于anyOf分支，反馈仍须给出可执行要求。
                if 'anyOf' in schema:
                    schema = next((s for s in schema['anyOf'] if s.get('type') != 'null'), schema)
                constraints = '；'.join(f'{k}={schema[k]}' for k in
                                       ('type', 'minLength', 'maxLength', 'minimum', 'maximum') if k in schema)
                message = f'{field}：取值不符合要求（{kind}）；请满足{constraints}，文本不能仅为空白。'
            else:
                message = 'arguments 必须为符合工具定义的 JSON 对象。'
            if message not in items:
                items.append(message)
        return prefix + '\n' + '\n'.join(items)
    return prefix + '\n' + str(error)


async def _run_memory_tool_loop(*, messages, executor, tools, prompt_version, tool_version,
                                tool_placement, audits) -> str | None:
    """两类节点共享反馈协议，但每次调用独立维护消息、纠错索引与计数。"""
    messages = deepcopy(messages)
    guidance_indices: list[int] = []
    failures = 0
    settings = get_settings().mllm
    generation = settings.generation
    async with MLLMClient(settings) as client:
        for round_number in range(1, _MAXIMUM_ROUNDS + 1):
            started = perf_counter()
            audit = {
                'round': round_number, 'prompt_version': prompt_version,
                'tool_version': tool_version, 'accepted': False,
            }
            audits.append(audit)
            try:
                response = await client.create_tool_chat_completion(
                    messages=deepcopy(messages), tools=deepcopy(list(tools)),
                    tool_choice='auto', tool_placement=tool_placement,
                    tool_task_index=1,
                    max_completion_tokens=generation.max_completion_tokens,
                    temperature=generation.temperature, top_p=generation.top_p,
                    top_k=generation.top_k, presence_penalty=generation.presence_penalty,
                    repetition_penalty=generation.repetition_penalty, seed=generation.seed,
                    enable_thinking=False,
                )
            except (MLLMRequestError, MLLMUnavailableError) as exc:
                audit.update(error=str(exc), elapsed_ms=(perf_counter() - started) * 1000)
                return '模型请求失败，处理未完成。'

            audit.update(
                response=deepcopy(response.assistant_message),
                tool_calls=[asdict(c) for c in response.tool_calls],
                completion=asdict(response.completion),
                elapsed_ms=(perf_counter() - started) * 1000,
            )
            calls = [{'id': c.call_id, 'type': 'function', 'function': {
                'name': c.name, 'arguments': c.arguments,
            }} for c in response.tool_calls]
            assistant = {'role': 'assistant', 'content': None, 'tool_calls': calls}
            try:
                if response.completion.finish_reason == 'length':
                    raise MemoryFlowViolation('本次输出被截断，不能作为成功结果；请缩短参数后重新调用一个工具。')
                if response.assistant_message.get('content') and response.assistant_message['content'].strip():
                    raise MemoryFlowViolation('工具调用之外不得输出普通文本，请只提交一个当前允许的工具调用。')
                if len(calls) == 1 and not response.tool_calls[0].call_id.strip():
                    raise MemoryFlowViolation('工具调用缺少有效标识，请重新生成真实工具调用。')
                feedback = executor.execute(calls)
            except ValueError as exc:
                failures += 1
                # 工具错误通过配对 tool 消息反馈；只有协议/流程违规才追加 user 指引。
                flow_violation = isinstance(exc, MemoryFlowViolation)
                if len(calls) == 1 and response.tool_calls[0].call_id.strip():
                    tool_feedback = {
                        'role': 'tool', 'tool_call_id': response.tool_calls[0].call_id,
                        'content': json.dumps({'ok': False, 'message':
                            '调用未接受，未改变任务状态。' if flow_violation else
                            _tool_error_message(exc, response.tool_calls[0].name, tools)}, ensure_ascii=False),
                    }
                    messages.extend([assistant, tool_feedback])
                    audit['feedback'] = deepcopy(tool_feedback)
                if flow_violation:
                    instruction = str(exc)
                    if len(calls) != 1 or response.assistant_message.get('content'):
                        instruction += '\n\n' + get_mllm_tool_tag()
                    guidance = build_system_guidence_message(instruction)
                    if 'feedback' in audit:
                        audit['tool_feedback'] = audit['feedback']
                    audit['feedback'] = deepcopy(guidance)
                    # 无合法单工具时只发流程指引，不伪造工具反馈。
                    guidance_indices.append(len(messages))
                    messages.append(guidance)
                if failures >= _MAXIMUM_CONSECUTIVE_FAILURES:
                    return '连续三轮调用未通过完整校验，处理失败。'
                continue

            # 本节点按会话契约只移除已解决的流程指引，不删除失败调用或工具反馈。
            # 记录程序插入的位置，避免按文本匹配误删历史中提到同名标记的内容。
            for index in reversed(guidance_indices):
                del messages[index]
            guidance_indices.clear()
            failures = 0
            audit.update(accepted=True, feedback=feedback.model_dump())
            # 成功交互按原顺序保留，think 中的后续安排也需要供下一轮继续使用。
            # 选择提交不作为上下文裁剪检查点；失败交互也不成为权威选择。
            messages.extend([assistant, {
                'role': 'tool', 'tool_call_id': response.tool_calls[0].call_id,
                'content': feedback.model_dump_json(),
            }])
            if executor.result is not None:
                return None
    return f'达到最大轮次 {_MAXIMUM_ROUNDS}，处理仍未完成。'


async def plan_memory_tasks(state: ConversationMemoryState) -> dict:
    """只有finish_selection成功才发布完整计划，原任务深拷贝后供分发使用。"""
    request = state['request']
    if isinstance(request, MemoryGenerationInput):
        request = request.model_dump()
    validated = MemoryGenerationInput.model_validate(request).model_copy(deep=True)
    executor = MemoryPlanningTools(validated)
    audits = []
    error = await _run_memory_tool_loop(
        messages=build_memory_planning_messages(validated), executor=executor,
        tools=MEMORY_PLANNING_TOOLS, prompt_version=MEMORY_PLANNING_PROMPT_VERSION,
        tool_version=MEMORY_PLANNING_TOOL_VERSION, tool_placement=MEMORY_PLANNING_TOOL_PLACEMENT,
        audits=audits,
    )
    result = MemoryGenerationOutput(
        execution_status='failed' if error else 'planned',
        plans=None if error else executor.result, error=error, planning_audit=tuple(audits),
    )
    return {'request': validated, **result.model_dump()}


async def process_memory_tasks(state: MemoryWorkerState) -> dict:
    """Send的单任务分支：独立会话完成整理，仅向reducer追加自己的终态。"""
    task = MemoryTaskInput.model_validate(state['task'].model_dump()).model_copy(deep=True)
    selection = SelectedMemoryTask.model_validate(state['selection'].model_dump())
    audits = []
    executor = MemorySummarizingTools()
    embedding_audit = None
    embedding = None
    try:
        error = await _run_memory_tool_loop(
            messages=build_memory_summarizing_messages(task, selection), executor=executor,
            tools=MEMORY_SUMMARIZING_TOOLS, prompt_version=MEMORY_SUMMARIZING_PROMPT_VERSION,
            tool_version=MEMORY_SUMMARIZING_TOOL_VERSION,
            tool_placement=MEMORY_SUMMARIZING_TOOL_PLACEMENT, audits=audits,
        )
        if not error and executor.result.retrieval_text is not None:
            # 正文工具完成后才开始编码。向量失败不重开模型对话，也不发布文本半成品。
            embedding_audit = {}
            embedding = await embed_memory_text(executor.result.retrieval_text, embedding_audit)
    except Exception as exc:
        # 分支异常不抹去兄弟任务结果；不回显任意异常正文。取消属于BaseException，继续传播。
        if embedding_audit is None:
            audits.append({'accepted': False, 'error_type': type(exc).__name__})
        error = f'{"向量化" if embedding_audit is not None else "任务整理"}异常（{type(exc).__name__}）'
    if error:
        result = MemoryTaskResult(task_id=task.task_id, task_number=selection.task_number,
                                  status='failed', error=error, audit=tuple(audits), embedding_audit=embedding_audit)
    else:
        extracted = executor.result
        result = MemoryTaskResult(
            task_id=task.task_id, task_number=selection.task_number,
            status='summarized' if extracted.retrieval_text is not None else 'no_memory',
            **extracted.model_dump(), embedding=embedding, audit=tuple(audits), embedding_audit=embedding_audit,
        )
    return {'task_results': [result]}


def collect_memory_results(state: ConversationMemoryState) -> dict:
    """节点3不调用模型；校验结果覆盖、唯一性和身份，再恢复原计划顺序。"""
    from app.agent.conversation_memory.tool import MemorySelectionResult

    request = state['request']
    request = MemoryGenerationInput.model_validate(request.model_dump() if isinstance(request, MemoryGenerationInput) else request)
    plans = MemorySelectionResult.model_validate(state['plans'])
    # 不能仅信任上游实例：model_copy可能绕过验证，汇总前重新按字段校验。
    items = [MemoryTaskResult.model_validate(r.model_dump() if isinstance(r, MemoryTaskResult) else r)
             for r in state.get('task_results', [])]
    source_ids = {t.task_id for t in request.tasks}
    selected_ids = {s.task_id for s in plans.selected_tasks}
    skipped_ids = set(plans.skipped_task_ids)
    numbers = [s.task_number for s in plans.selected_tasks]
    if (selected_ids & skipped_ids or selected_ids | skipped_ids != source_ids
            or len(skipped_ids) != len(plans.skipped_task_ids)
            or len(selected_ids) != len(plans.selected_tasks) or numbers != sorted(set(numbers))):
        raise ValueError('筛选计划必须完整且互斥地覆盖原任务')
    for selection in plans.selected_tasks:
        if selection.task_number > len(request.tasks) or request.tasks[selection.task_number - 1].task_id != selection.task_id:
            raise ValueError('筛选计划的任务身份或序号与输入不一致')
    by_id = {r.task_id: r for r in items}
    expected = {s.task_id for s in plans.selected_tasks}
    if len(by_id) != len(items) or set(by_id) != expected:
        raise ValueError('并发整理结果缺失、重复或包含计划外任务，拒绝发布汇总')
    ordered = tuple(by_id[s.task_id] for s in plans.selected_tasks)
    failures = sum(r.status == 'failed' for r in ordered)
    status = ('failed' if failures and failures == len(ordered)
              else 'partial_failed' if failures else 'summarized')
    pending = []
    for task in request.tasks:
        item = by_id.get(task.task_id)
        if item is not None and item.status == 'failed':
            continue
        # 跳过向量化不等于跳过历史备份。始终复制原payload，含附件路径及完整有序轨迹。
        pending.append(MemoryPendingRecord(
            **deepcopy(task.model_dump()),
            retrieval_text=item.retrieval_text if item else None,
            embedding=item.embedding if item else None,
        ))
    result = MemoryGenerationOutput(
        execution_status=status, plans=plans, results=ordered, pending_records=tuple(pending),
        error=f'{failures}/{len(ordered)}个已选任务加工失败' if failures else None,
        planning_audit=state.get('planning_audit', ()),
    )
    return result.model_dump()
