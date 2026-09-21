"""主题规划与 Map 生成已接入，Reduce 负责全量验收。"""
from langgraph.types import Send
from ..input_filter import filter_summary_tasks
from .planning import previous_topics, validate_topic_plan
from .planner import run_topic_planner
from .generator import run_topic_generator
from ..compression import FIFOCompressionRequest
from .schema import TopicPlan, TopicSummary, TopicGenerationRequest, TopicMapResult, FIFOTopicSummary, FIFOSummaryResult


async def plan_topics_placeholder(request):
    raise NotImplementedError('主题划分模型尚未接入')


async def generate_topic_placeholder(request):
    raise NotImplementedError('单主题摘要模型尚未接入')


async def plan_topics(state, *, planner):
    try:
        request = FIFOCompressionRequest.model_validate(state['request']).model_copy(deep=True)
        request = request.model_copy(update={'tasks': filter_summary_tasks(request.tasks)})
        previous_topics(request.summary)
        ids = [t.task_id for t in request.tasks]
        if (not ids or len(ids) != len(set(ids)) or ids != request.scope.task_ids
                or ids[0] != request.scope.start_task_id or ids[-1] != request.scope.end_task_id
                or request.tasks[-1].status != 'completed' or any(t.status == 'active' for t in request.tasks)):
            raise ValueError('范围非法')
    except (ValueError, TypeError, KeyError):
        return {'error_type': 'input_error', 'error': '摘要输入与完整任务范围不一致。'}
    try:
        audit = []
        raw = await run_topic_planner(request.model_copy(deep=True), audit=audit) if planner is run_topic_planner else await planner(request.model_copy(deep=True))
        plan = validate_topic_plan(raw, request)
        return {'validated': request, 'plan': plan, 'planning_audit': audit}
    except NotImplementedError:
        return {'error_type': 'not_implemented', 'error': 'FIFO 主题划分模型尚未接入。'}
    except Exception:
        return {'error_type': 'generation_error', 'error': 'FIFO 主题划分或范围验收失败。', 'planning_audit': locals().get('audit', [])}


def dispatch_topics(state):
    if state.get('error') or not state['plan'].topics:
        return 'reduce_topic_summaries'
    request = state['validated']
    return [Send('generate_topic', {'topic_request': TopicGenerationRequest(
        topic=topic.model_copy(deep=True),
        tasks=[task.model_copy(deep=True) for task in request.tasks if task.task_id in topic.task_ids],
        previous_summary={'topics': [previous_topics(request.summary)[key].model_dump(mode='json') for key in topic.previous_topic_ids]} if topic.previous_topic_ids else None,
    )}) for topic in state['plan'].topics]


def validate_topic_summary(summary, topic):
    if summary.topic_id != topic.topic_id or summary.title != topic.title:
        raise ValueError('结果主题不一致')
    for field in ('current_memory', 'open_items'):
        for item in getattr(summary, field):
            if len(item.task_ids) != len(set(item.task_ids)) or not set(item.task_ids) <= set(topic.task_ids):
                raise ValueError('条目引用越界')


async def generate_topic(state, *, generator):
    request = state['topic_request']
    audit = []
    try:
        raw = await run_topic_generator(request.model_copy(deep=True), audit=audit) if generator is run_topic_generator else await generator(request.model_copy(deep=True))
        summary = TopicSummary.model_validate(raw)
        validate_topic_summary(summary, request.topic)
        return {'generation_audit': [{'topic_id': request.topic.topic_id, 'events': audit}], 'topic_results': [TopicMapResult(topic_id=summary.topic_id, status='success', summary=summary)]}
    except NotImplementedError:
        return {'generation_audit': [{'topic_id': request.topic.topic_id, 'events': audit}], 'topic_results': [TopicMapResult(topic_id=request.topic.topic_id, status='error', error_type='not_implemented')]}
    except Exception:
        return {'generation_audit': [{'topic_id': request.topic.topic_id, 'events': audit}], 'topic_results': [TopicMapResult(topic_id=request.topic.topic_id, status='error', error_type='generation_error')]}


def reduce_topic_summaries(state):
    """唯一尾节点：全量验收后一次性发布，不额外调用模型或继承旧主题。"""
    if state.get('error'):
        return {'result': FIFOSummaryResult(status='error', error_type=state['error_type'], error_feedback=state['error'])}
    kind = 'generation_error'
    try:
        rows = [TopicMapResult.model_validate(row) for row in state.get('topic_results', [])]
        plan = state['plan'].topics
        indexed = {row.topic_id: row for row in rows}
        if len(rows) != len(plan) or len(indexed) != len(rows) or set(indexed) != {t.topic_id for t in plan}:
            raise ValueError('分支数量或标识不一致')
        if any(row.status == 'error' for row in rows):
            kind = 'not_implemented' if any(row.error_type == 'not_implemented' for row in rows) else kind
            raise ValueError('存在失败分支')
        topics = []
        for topic in plan:
            summary = indexed[topic.topic_id].summary
            # Reduce 再次验收边界，避免注入实现或恢复状态绕过 Map 校验。
            validate_topic_summary(summary, topic)
            topics.append(summary)
        merged = FIFOTopicSummary(latest_coverage=state['validated'].scope, topics=topics)
        return {'result': FIFOSummaryResult(status='success', summary=merged.model_dump(mode='json'))}
    except (ValueError, TypeError, KeyError, AttributeError):
        return {'result': FIFOSummaryResult(status='error', error_type=kind, error_feedback='主题摘要未全部通过验收，不发布部分结果。')}
