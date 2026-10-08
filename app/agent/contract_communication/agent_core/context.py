"""Agent Core 入口上下文装配；消费一致快照，不维护第二份会话存储。"""
from dataclasses import dataclass
from copy import deepcopy
import json

from app.schema.communication import ConversationHistoryRecord
from app.schema.communication_workspace import WorkspaceSnapshot
from app.schema.reasoning_window import ReasoningWindow
from .context_rendering import render_summary_section, render_workspace_section, render_task_input, TaskRenderInput
from .subgraph.fifo_management.fifo_summary.schema import FIFOTopicSummary
from .native_messages import validate_native_messages, native_trace_entries, close_native_messages
from .subgraph.fifo_management.schema import FIFOTask


@dataclass(frozen=True)
class AgentCoreContext:
    workspace: WorkspaceSnapshot
    summary: dict | None
    tasks: tuple[TaskRenderInput, ...]
    text: str  # messages 的 JSON 诊断视图；不是给模型的整体渲染文本。
    messages: tuple[dict, ...]  # 动态上下文的实际消息序列；完整 system/tools 仍由主循环装配。
    fifo: tuple[FIFOTask, ...]


def _trace_and_final(record):
    trace = record.payload.get('trace', [])
    # 分片可能被工具条目隔开；按消息标识合并，按首次出现的位置展示。
    messages = {}
    for item in trace:
        if item['type'] == 'message':
            key = item['message_id']
            if key not in messages:
                messages[key] = {**item, 'text': ''}
            messages[key]['text'] += item['text']
            messages[key]['status'] = item['status']
            messages[key]['references'] = deepcopy(item.get('references', []))
    accepted_finals = [m for m in messages.values()
                       if m['message_kind'] == 'final' and m['status'] == 'completed']
    if len(accepted_finals) > 1:
        raise ValueError('同一任务包含多个已完成最终输出')
    final_message = accepted_finals[0] if accepted_finals else None
    failed_calls = {i['call_id'] for i in trace if i['type'] == 'tool_result' and i['status'] != 'succeeded'}
    entries, seen = [], set()
    for item in trace:
        kind = item['type']
        if kind in {'tool_call', 'tool_result'}:
            # 历史公开轨迹中的失败反馈不是模型纠错记忆，不注入已结束任务上下文。
            if item['call_id'] in failed_calls:
                continue
            content = json.dumps({k: v for k, v in item.items()
                                  if k not in {'type', 'sequence', 'call_id', 'name'}}, ensure_ascii=False)
            entries.append({'kind': kind, 'call_id': item['call_id'],
                            'name': item.get('name'), 'content': content})
        elif kind == 'message':
            key = item['message_id']
            if key in seen:
                continue
            seen.add(key)
            message = messages[key]
            if message is final_message or message['status'] != 'completed' or not message['text'].strip():
                continue
            entries.append({'kind': 'intermediate', 'content': message['text']})
        elif kind == 'system_guidence':
            # 已结束任务不再携带操作提示；也保护来自旧驻留/持久化记录的输入。
            continue
        else:
            raise ValueError('不支持的会话轨迹类型，不能静默忽略')
    ending = None
    if record.status != 'processing':
        kind = {'completed': 'completed', 'cancelled': 'interrupted',
                'superseded': 'superseded', 'failed': 'failed'}[record.status]
        ending = {'kind': kind}
        if final_message is not None:
            ending['content'] = final_message['text']
        # completed 缺失最终正文时由渲染契约显式拒绝，不能编造历史答案。
    if 'agent_messages' in record.payload:
        native = record.payload['agent_messages']
        # 原生消息是主助手权威轨迹；公开投影不再次拼入，避免中途输出与参数重复。
        entries = native_trace_entries(native, has_final_output=final_message is not None)
    return entries, ending


def assemble_agent_core_context(*, workspace: WorkspaceSnapshot, summary: dict | None,
                                records: tuple[ConversationHistoryRecord, ...], current_turn_id: str,
                                reasoning_window: ReasoningWindow | None = None) -> AgentCoreContext:
    """历史层已选取最新摘要之后的任务；当前任务必须位于末尾且仍在处理。"""
    workspace = WorkspaceSnapshot.model_validate(workspace).model_copy(deep=True)
    reasoning_window = ReasoningWindow.model_validate(reasoning_window) if reasoning_window is not None else ReasoningWindow()
    if not records or records[-1].turn_id != current_turn_id or records[-1].status != 'processing':
        raise ValueError('当前用户请求必须是上下文末尾的处理任务')
    sequences = [record.sequence for record in records]
    if sequences != sorted(set(sequences)) or len({r.record_id for r in records}) != len(records):
        raise ValueError('任务必须按唯一序号排列且身份不重复')
    if sum(r.turn_id == current_turn_id for r in records) != 1:
        raise ValueError('当前任务只能出现一次')
    tasks = []
    for record in records:
        if record.kind != 'task' or (record.turn_id != current_turn_id and record.status not in {
                'completed', 'cancelled', 'superseded', 'failed'}):
            raise ValueError('上下文含不允许的任务状态')
        source = record.payload['input']
        files = []
        for file in source.get('files', []):
            # 未准入附件不能因为存在文件名或摘要而进入二层上下文。
            if file.get('admission') != 'accepted':
                continue
            files.append({key: file.get(key) for key in
                          ('file_id', 'file_name', 'display_name', 'summary', 'page_count')})
        if record.turn_id == current_turn_id:
            entries, ending = [], None
        else:
            entries, ending = _trace_and_final(record)
        tasks.append(TaskRenderInput.model_validate({
            'task_number': record.sequence, 'task_id': record.record_id, 'created_at': record.created_at,
            'user_input': {'content': source.get('text'), 'files': files, 'contracts': source.get('contracts', [])},
            'execution_trace': entries, 'final_output': ending}))
    if summary is not None:
        summary = FIFOTopicSummary.model_validate(summary).model_dump()
    summary_text = render_summary_section(summary)
    messages = []
    if summary_text:
        messages.append({'role': 'user', 'content': summary_text})
    fifo = []
    for record, task in zip(records, tasks, strict=True):
        active = record.turn_id == current_turn_id
        task_messages = [{'role': 'user', 'content': render_task_input(
            task.model_copy(update={'final_output': None}))}]
        if 'agent_messages' in record.payload:
            # 历史与当前任务使用同一原生契约；历史清除程序提示，但保留真实调用和回执。
            validate = validate_native_messages if active else close_native_messages
            native = validate(record.payload['agent_messages'])
            task_messages.extend(reasoning_window.inject(native, task_id=task.task_id))
            # 兼容已有工具轨迹、但通过服务接口直接结束的旧任务，保留其真实最终正文。
            if (not active and task.final_output.content is not None and not any(
                    call['function']['name'] == 'finish_task'
                    for message in native for call in message.get('tool_calls', []))):
                task_messages.append({'role': 'assistant', 'content': task.final_output.content})
        elif not active:
            # 旧记录只有展示投影，不能把 input_summary 猜成真实工具参数。
            # 仅逐条提供明确标注的历史说明，实际输出仍使用 assistant 角色。
            for entry in task.execution_trace:
                content = entry.content
                if entry.kind in {'tool_call', 'tool_result'}:
                    content = f'旧记录的工具展示摘要（非原始调用）：{entry.name or entry.kind}\n{content}'
                task_messages.append({'role': 'assistant', 'content': content})
            if task.final_output.content is not None:
                task_messages.append({'role': 'assistant', 'content': task.final_output.content})
        if not active:
            # 终态是程序记录，不伪造助手结论；摘要按 FIFOTask.status 获知状态。
            labels = {'completed': '已完成', 'cancelled': '用户终止',
                      'superseded': '用户调整方向', 'failed': '执行失败'}
            task_messages.append({'role': 'user', 'source': 'system', 'content':
                f'任务结束记录：{json.dumps(task.task_id, ensure_ascii=False)}；{labels[record.status]}。这是历史状态，不是新的用户请求。'})
        else:
            # 工作区独立注入且紧邻本轮输入，不混入 FIFO 任务或重复计数。
            messages.append({'role': 'user', 'content': render_workspace_section(workspace.payload)})
        messages.extend({key: value for key, value in message.items() if key != 'source'}
                        for message in task_messages)
        fifo.append(FIFOTask(task_id=task.task_id,
            status='active' if active else {'completed': 'completed', 'cancelled': 'interrupted',
                'superseded': 'interrupted', 'failed': 'failed'}[record.status],
            messages=deepcopy(task_messages)))
    # text 仅供诊断兼容；正式请求始终使用 messages，禁止将其作为整体 user 注入。
    text = json.dumps(messages, ensure_ascii=False)
    return AgentCoreContext(workspace=workspace, summary=deepcopy(summary), tasks=tuple(tasks),
                            text=text, messages=tuple(deepcopy(messages)), fifo=tuple(fifo))


def validate_closed_agent_task(record: ConversationHistoryRecord) -> None:
    """终态提交前验收历史表示；不另存渲染文本，下一次装配直接选入历史区。"""
    if not record.payload.get('agent_core_ready'):
        return
    entries, ending = _trace_and_final(record)
    source = record.payload['input']
    files = [{key: file.get(key) for key in ('file_id', 'file_name', 'display_name', 'summary', 'page_count')}
             for file in source.get('files', []) if file.get('admission') == 'accepted']
    TaskRenderInput.model_validate({'task_number': record.sequence, 'task_id': record.record_id, 'created_at': record.created_at,
                         'user_input': {'content': source.get('text'), 'files': files, 'contracts': source.get('contracts', [])},
                         'execution_trace': entries, 'final_output': ending})
