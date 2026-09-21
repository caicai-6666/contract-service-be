"""FIFO 管理节点；未满分支执行及反馈已实现，自动压缩显式占位。"""
import json
from .schema import FIFOManagementRequest, FIFOManagementResult, FIFORange, FIFOExecutionResult, FIFOGuidance
from ...prompt.guidance import build_system_guidence_message
from .compression import compress_fifo, summarize_fifo_placeholder, count_fifo_summary_tokens
from .token_count import count_fifo_task_tokens, FIFO_RENDER_VERSION


def select_completed_prefix(tasks, task_tokens):
    """按 70% 所在任务向右对齐，若未完成则回退；不跨越活动任务。"""
    if not isinstance(task_tokens, list) or len(tasks) != len(task_tokens) or any(type(n) is not int or n < 0 for n in task_tokens):
        raise ValueError('任务计数必须与 FIFO 一一对应且为非负整数')
    total = sum(task_tokens)
    if total == 0:
        return None
    count = 0
    target = 0
    for target, n in enumerate(task_tokens):
        count += n
        if count * 10 >= total * 7:
            break
    # 活动任务不可压缩；输入契约保证活动任务只在尾部。
    end = next((i for i in range(target, -1, -1) if tasks[i].status == 'completed'), None)
    if end is None:
        return None
    return FIFORange(start_task_id=tasks[0].task_id, end_task_id=tasks[end].task_id,
                     task_ids=[t.task_id for t in tasks[:end + 1]], tokens=sum(task_tokens[:end + 1]))


async def count_fifo_tokens(state, *, counter=count_fifo_task_tokens):
    """节点 1：默认接口逐任务计数，输出真实记账用量与完整任务压缩范围。"""
    try:
        request = FIFOManagementRequest.model_validate(state['request']).model_copy(deep=True)
    except (ValueError, TypeError, KeyError):
        return {'error': 'FIFO 输入不符合任务与操作契约。', 'result_type': 'input_error'}
    base = {'validated': request, 'execution_status': 'not_executed', 'system_guidence': []}
    if counter is None:
        return {**base, 'error': 'FIFO token 计数不可用，本步操作未执行。', 'result_type': 'capacity_error'}
    try:
        counts = await counter([task.model_copy(deep=True) for task in request.fifo])
        scope = select_completed_prefix(request.fifo, counts)
    except Exception:
        return {**base, 'error': 'FIFO token 计数失败，本步操作未执行。', 'result_type': 'capacity_error'}
    return {**base, 'tokens': sum(counts), 'task_tokens': counts, 'compression_range': scope,
            'count_render_version': FIFO_RENDER_VERSION}


def choose_capacity_branch(state, *, token_budget):
    """节点 2：内部实际用量达到 95% 即视为满容量，模型只接收 100% 状态。"""
    if state.get('error'):
        return {'branch': 'error'}
    tokens = state['tokens']
    return {'branch': 'compress' if tokens * 20 >= token_budget * 19 else 'execute',
            'warning': tokens * 5 >= token_budget * 4}


async def compress_before_execution(state, *, token_budget, counter=count_fifo_task_tokens,
                                    compressor=summarize_fifo_placeholder, summary_counter=count_fifo_summary_tokens, summary_commit=None):
    return await compress_fifo(state, phase='before_execution', token_budget=token_budget,
                               counter=counter, compressor=compressor, summary_counter=summary_counter, summary_commit=summary_commit)


async def execute_after_compression(state, *, executor=None, supports_foldable=False):
    """仅在压缩成功后执行一次；复用统一执行入口，压缩失败直接收尾。"""
    if state.get('error'):
        return {}
    return await execute_without_compression(state, executor=executor, supports_foldable=supports_foldable)


async def execute_without_compression(state, *, executor=None, supports_foldable=False):
    """执行一次可信适配；它负责工具校验、审计、权限和提交，图不猜测执行状态。"""
    if executor is None:
        return {'error': 'FIFO 工具执行适配尚未接入，本步操作未执行。', 'result_type': 'not_implemented'}
    request = state.get('candidate', state['validated'])
    try:
        result = FIFOExecutionResult.model_validate(await executor(request.operation.model_copy(deep=True)))
        # 工具结果必须能按真实 JSON 渲染，拒绝半成品和非标准数值。
        content = json.dumps(result.tool_result, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except Exception:
        # 已经进入执行器，无法推断是否产生副作用，禁止报告未执行或自动重试。
        return {'execution_status': 'unknown', 'error': '工具执行状态无法确认，请先核实，不要重复执行。',
                'result_type': 'tool_error'}
    values = {'execution_status': result.status, 'tool_result': result.tool_result, 'content': result.content,
              'system_guidence': [*state.get('system_guidence', []), *result.system_guidence]}
    if result.status != 'succeeded':
        # 失败反馈交给外部有限纠错流程，不写入正常 FIFO，避免污染权威历史。
        return {**values, 'error': '工具执行失败。' if result.status == 'failed' else '工具执行状态无法确认，请先核实。',
                'result_type': 'tool_error'}
    if result.content.type == 'foldable' and not supports_foldable:
        # 独立图可能绕过注册宿主；保留执行事实和页面引用，但不伪造已展示的反馈。
        return {**values, 'error': '页面展示尚未接入，本次结果已保留，不能继续生成或重放操作。',
                'result_type': 'unsupported_content'}
    from ...context_rendering.page import render_tool_receipt
    content = render_tool_receipt(result)
    candidate = request.model_copy(deep=True)
    candidate.fifo[-1].messages.append({'role': 'tool', 'tool_call_id': request.operation.call_id, 'content': content})
    for guidance in [*state.get('pending_compression_guidence', []), *result.system_guidence]:
        candidate.fifo[-1].messages.append({**guidance.message, 'source': guidance.source})
    return {**values, 'candidate': candidate, 'pending_compression_guidence': [], 'result_type': 'tool_result'}


def _capacity_guidance(*, full, scope):
    """生成供模型读取的管理状态，不暴露内部 95% 或真实 N/B。"""
    if full:
        reason = 'FIFO 可用容量已耗尽（100%）。本步工具已执行成功。'
        action = '下一次工具调用将先自动压缩再执行，包括工作区操作；不要重放本步已成功的操作。'
    else:
        reason = 'FIFO 使用量已达到 80% 整理提醒阈值。本步工具已执行成功。'
        action = '暂停新的业务探索，仅提取指定任务范围内仍然有效的关键内容，使用工作区工具保存。未及时保存的信息可能在自动压缩后丢失。'
    if scope is not None:
        action += ' 本次范围：从 task_id=' + json.dumps(scope.start_task_id, ensure_ascii=False) + ' 到 task_id=' + json.dumps(scope.end_task_id, ensure_ascii=False) + '（包含首尾完整任务，末项 completed）；不得扩大范围。'
    else:
        action += ' 当前没有以 completed 任务结尾的可整理前缀，不得自行猜测范围或驱逐活动任务。'
    return FIFOGuidance(source='fifo', message=build_system_guidence_message(
        kind='context_capacity', reason=reason, required_action=action))


async def recheck_after_execution(state, *, token_budget, counter=count_fifo_task_tokens):
    """计入本步结果和所有提示，再确定尾部信号；提示自身跨阈值时重新生成。

    每次从未追加 FIFO 提示的同一快照构造，避免循环追加告警撑满容量。
    最多四轮收敛，无法确认最终提示与范围一致则禁止下一次模型请求。
    """
    if state.get('error'):
        return {}
    token_budget = state.get('fifo_budget', token_budget)
    base = state['candidate']
    latest = base
    try:
        async def measure(candidate):
            counts = await counter([task.model_copy(deep=True) for task in candidate.fifo])
            scope = select_completed_prefix(candidate.fifo, counts)
            return sum(counts), scope
        tokens, scope = await measure(base)
        warning = state.get('warning', False) or tokens * 5 >= token_budget * 4
        if not warning:
            return {'tokens': tokens, 'compression_range': scope, 'can_continue': True, 'post_full': False}
        for _ in range(4):
            full = tokens * 20 >= token_budget * 19
            guidance = _capacity_guidance(full=full, scope=scope)
            latest = base.model_copy(deep=True)
            latest.fifo[-1].messages.append({**guidance.message, 'source': guidance.source})
            final_tokens, final_scope = await measure(latest)
            # 范围按包含告警的最终 FIFO 计算，阈值和范围均稳定才发布提示。
            if (final_tokens * 20 >= token_budget * 19) == full and final_scope == scope:
                return {'candidate': latest, 'tokens': final_tokens, 'compression_range': final_scope,
                        'system_guidence': [*state.get('system_guidence', []), guidance],
                        'can_continue': final_tokens < token_budget, 'post_full': final_tokens >= token_budget}
            tokens, scope = final_tokens, final_scope
    except Exception:
        return {'error': '工具已成功，但 FIFO 执行后计数失败；暂停后续模型请求，不要重复执行。',
                'result_type': 'capacity_error'}
    return {'error': '工具已成功，但 FIFO 提示与容量范围未能稳定；暂停后续模型请求，不要重复执行。',
            'result_type': 'capacity_error'}


async def compress_after_execution(state, *, token_budget, counter=count_fifo_task_tokens,
                                   compressor=summarize_fifo_placeholder, summary_counter=count_fifo_summary_tokens, summary_commit=None):
    return await compress_fifo(state, phase='after_execution', token_budget=token_budget,
                               counter=counter, compressor=compressor, summary_counter=summary_counter, summary_commit=summary_commit)


def route_after_execution(state):
    if state.get('error'):
        return 'final'
    return 'compress' if state.get('post_full') else 'final'


def build_final_result(state):
    """节点 5 是唯一出口，分离管理状态与动作状态，失败默认禁止继续。"""
    request = state.get('candidate', state.get('validated'))
    error = state.get('error')
    result = FIFOManagementResult(
        status='error' if error else 'success',
        execution_status=state.get('execution_status', 'not_executed'),
        result_type=state.get('result_type', 'tool_result'),
        tool_result=state.get('tool_result'),
        content=state.get('content', {'type': 'ordinary'}),
        fifo=request.fifo if request else [], summary=request.summary if request else None,
        system_guidence=state.get('system_guidence', []), compression_range=state.get('compression_range'),
        can_continue=False if error else state.get('can_continue', False), error_feedback=error,
        fifo_budget_tokens=state.get('fifo_budget'),
    )
    return {'result': result}
