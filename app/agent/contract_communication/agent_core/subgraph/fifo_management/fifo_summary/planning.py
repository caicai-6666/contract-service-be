"""联合主题规划校验，适用于真实模型及注入测试规划器。"""
from .schema import TopicPlan, TopicSummary, LegacyTopicSummary


def previous_topics(summary):
    if summary is None:
        return {}
    if not isinstance(summary, dict) or not isinstance(summary.get('topics'), list):
        raise ValueError('旧摘要缺少 topics，需先迁移结构，不能忽略旧内容')
    version = summary.get('version')
    if version not in (None, 'fifo-topic-summary-v1', 'fifo-topic-summary-v2'):
        raise ValueError('不支持的旧摘要版本')
    topics = []
    for value in summary['topics']:
        # 仅在输入边界兼容五区域，保留历史分区，不把历史要求自动升级成当前要求。
        model = (LegacyTopicSummary if version == 'fifo-topic-summary-v1' else TopicSummary
                 if version == 'fifo-topic-summary-v2' else
                 TopicSummary if isinstance(value, dict) and 'current_memory' in value else LegacyTopicSummary)
        topics.append(model.model_validate(value))
    result = {t.topic_id: t for t in topics}
    if len(result) != len(topics):
        raise ValueError('旧摘要主题标识重复')
    return result


def validate_topic_plan(value, request):
    plan = TopicPlan.model_validate(value)
    old = set(previous_topics(request.summary))
    tasks = {t.task_id for t in request.tasks}
    if len({t.topic_id for t in plan.topics}) != len(plan.topics):
        raise ValueError('topics.topic_id 必须唯一')
    for topic in plan.topics:
        for name, allowed in [('task_ids', tasks), ('previous_topic_ids', old)]:
            refs = getattr(topic, name)
            if len(refs) != len(set(refs)) or not set(refs) <= allowed:
                raise ValueError(f'topics.{name} 必须是不重复的有效来源标识')
        if not topic.task_ids and not topic.previous_topic_ids:
            raise ValueError('每个主题至少需要一种实际来源')
    return plan
