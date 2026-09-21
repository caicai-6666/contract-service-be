"""自动压缩外围契约：核心摘要器占位，范围/容量验收与候选提交由程序完成。"""
import json
from copy import deepcopy
from typing import Literal
from pydantic import Field
from app.schema.communication_workspace import WorkspaceObject
from app.core.config import get_settings
from app.infrastructure.vllm_tokenizer import count_text_tokens
from .schema import FIFOTask, FIFORange, FIFOGuidance
from .input_filter import filter_summary_tasks
from ...prompt.guidance import build_system_guidence_message


class FIFOCompressionRequest(WorkspaceObject):
    summary: dict | None = Field(description='已有累计摘要；返回值需合并旧摘要与本次有效业务交互。')
    tasks: list[FIFOTask] = Field(description='选定前缀的业务交互副本，系统操作提示已过滤；无活动任务或待执行操作。')
    scope: FIFORange = Field(description='程序确认的完整任务范围，末项必须 completed。')
    phase: Literal['before_execution', 'after_execution'] = Field(description='调用发生在本步工具执行之前或之后，不授权摘要器执行工具。')


class FIFOCompressionResult(WorkspaceObject):
    summary: dict = Field(min_length=1, description='合并后的非空结构化累计摘要，不返回或改写剩余 FIFO；精确摘要字段另行定义。')


async def summarize_fifo_placeholder(request: FIFOCompressionRequest) -> FIFOCompressionResult:
    """真正的多轮自动摘要核心在此接入；默认不能返回伪造摘要。"""
    # 延迟导入避免两个子图的契约引用形成初始化循环。
    from .fifo_summary import build_fifo_summary_subgraph
    result = (await build_fifo_summary_subgraph().ainvoke({'request': request}))['result']
    if result.status == 'error':
        if result.error_type == 'not_implemented':
            raise NotImplementedError(result.error_feedback)
        raise ValueError(result.error_feedback)
    return FIFOCompressionResult(summary=result.summary)


async def count_fifo_summary_tokens(summary: dict | None) -> int:
    if summary is None:
        return 0
    text = json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    return await count_text_tokens(text, get_settings().mllm)


async def compress_fifo(state, *, phase, token_budget, counter, compressor, summary_counter, summary_commit=None):
    """所有校验通过才发布候选；工具已执行时失败也不回滚或重放工具。"""
    from .node import select_completed_prefix
    current = state.get('candidate', state['validated'])
    scope = state.get('compression_range')
    if scope is None:
        return {'error': 'FIFO 可用容量已耗尽（100%），没有以 completed 任务结尾的安全压缩范围。', 'result_type': 'capacity_error'}
    try:
        size = len(scope.task_ids)
        prefix = current.fifo[:size]
        if (not prefix or size >= len(current.fifo) or prefix[-1].status != 'completed'
                or any(t.status == 'active' for t in prefix)
                or [t.task_id for t in prefix] != scope.task_ids
                or prefix[0].task_id != scope.start_task_id or prefix[-1].task_id != scope.end_task_id):
            raise ValueError('压缩范围与当前 FIFO 不一致')
        request = FIFOCompressionRequest(summary=current.summary, tasks=filter_summary_tasks(prefix), scope=scope, phase=phase)
        output = FIFOCompressionResult.model_validate(await compressor(request.model_copy(deep=True)))
        json.dumps(output.summary, ensure_ascii=False, allow_nan=False)
        old_tokens = await summary_counter(deepcopy(current.summary))
        new_tokens = await summary_counter(deepcopy(output.summary))
        if any(type(n) is not int or n < 0 for n in (old_tokens, new_tokens)):
            raise ValueError('摘要计数非法')
        # 原 FIFO 配额已经扣除旧摘要，摘要变更后只在轨迹区内部重新分配。
        budget = state.get('fifo_budget', token_budget) + old_tokens - new_tokens
        if budget <= 0:
            raise ValueError('摘要耗尽轨迹预算')
        candidate = current.model_copy(deep=True, update={'fifo': [t.model_copy(deep=True) for t in current.fifo[size:]],
                                                         'summary': deepcopy(output.summary)})
        notice = FIFOGuidance(source='fifo', message=build_system_guidence_message(
            kind='context_capacity', reason='FIFO 已触发自动压缩，指定已完成任务已汇入累计摘要并移出近期轨迹。',
            required_action='原先针对已移出任务的整理范围已失效。依据最新累计摘要、剩余轨迹及工作区继续；本步工具结果单独确认，不要重放已成功操作。'))
        # 执行前不在待处理 assistant/tool 配对之间插入提示，先投影计数，执行后追加。
        projection = candidate.model_copy(deep=True)
        projection.fifo[-1].messages.append({**notice.message, 'source': notice.source})
        counts = await counter(projection.fifo)
        new_scope = select_completed_prefix(projection.fifo, counts)
        total = sum(counts)
        if total * 20 >= budget * 19:
            raise ValueError('压缩后仍无可继续执行的容量')
        if phase == 'after_execution':
            candidate = projection
        if summary_commit is not None:
            await summary_commit(deepcopy(current.summary), deepcopy(output.summary), scope.model_copy(deep=True))
        return {'candidate': candidate, 'tokens': total, 'compression_range': new_scope,
                'fifo_budget': budget, 'warning': total * 5 >= budget * 4,
                'system_guidence': [*state.get('system_guidence', []), notice],
                'pending_compression_guidence': [notice] if phase == 'before_execution' else [],
                'can_continue': phase == 'after_execution', 'post_full': False}
    except NotImplementedError:
        return {'error': 'FIFO 自动摘要核心尚未接入；保留当前轨迹和已确认工具状态。', 'result_type': 'not_implemented'}
    except Exception:
        return {'error': 'FIFO 压缩或验收失败；保留当前轨迹、摘要及已确认工具状态，不要重放成功操作。', 'result_type': 'capacity_error'}
