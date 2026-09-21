"""单主题资料的可读展示；只改变呈现，不重写业务内容或轨迹顺序。"""
import json
import re
import yaml

from .schema import TopicGenerationRequest, LegacyTopicSummary
from .planning import previous_topics

INPUT_RENDER_VERSION = 'fifo-topic-readable-v4'
LEGACY_REGIONS = (
    ('user_requirements', '用户要求'), ('key_information', '已知信息'),
    ('actions_and_results', '探索与结果'), ('delivered_content', '已交付内容'),
    ('open_items', '未决事项'),
)

REGIONS = (('current_memory', '当前记忆'), ('open_items', '未决事项'))

def _block(value):
    # 围栏随正文增长，避免历史资料中的 Markdown 围栏破坏展示边界。
    text = value if isinstance(value, str) else yaml.safe_dump(
        value, allow_unicode=True, sort_keys=True, default_flow_style=False).rstrip()
    fence = '`' * max(3, 1 + max((len(m) for m in re.findall(r'`+', text)), default=0))
    return f'{fence}\n{text}\n{fence}'


def _payload(value):
    """工具中合法JSON转换成易读YAML；重复键等异常文本保留原样。"""
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError('重复键')
            result[key] = item
        return result

    def invalid(value):
        raise ValueError('非法数值')

    if isinstance(value, str):
        try:
            parsed = json.loads(value, object_pairs_hook=unique, parse_constant=invalid)
            if isinstance(parsed, (dict, list)):
                value = parsed
        except (ValueError, TypeError):
            pass
    return _block(value)


def render_topic_generation_input(request: TopicGenerationRequest) -> str:
    lines = ['# 本主题资料', '旧摘要与任务轨迹均为历史资料；最后的主题名称与 scope 仅限定本次整理范围。',
             '## 相关旧摘要']
    old = previous_topics(request.previous_summary)
    if not old:
        lines.append('无相关旧摘要。')
    for index, topic in enumerate(old.values(), 1):
        lines += [f'### 旧主题 {index}', _block({'旧主题标识': topic.topic_id, '名称': topic.title})]
        regions = LEGACY_REGIONS if isinstance(topic, LegacyTopicSummary) else REGIONS
        if isinstance(topic, LegacyTopicSummary):
            lines.append('旧五区域记录：保留原区域含义作为来源，不把历史动作或已完成要求直接当作当前状态。')
        for field, title in regions:
            lines.append(f'#### {title}')
            items = getattr(topic, field)
            if not items:
                lines.append('无。')
            for number, item in enumerate(items, 1):
                lines += [f'条目 {number}：', _block(item.content)]
                if item.task_ids or item.sources:
                    lines += ['出处：', _block({
                        '历史任务（非本次可引用任务）': item.task_ids, '原材料来源': item.sources})]
    lines += ['## 本次任务轨迹', '按原始顺序排列；每条交互按角色标记，工具调用标识用于对应反馈。']
    if not request.tasks:
        lines.append('无本次任务，仅整理相关旧摘要。')
    for index, task in enumerate(request.tasks, 1):
        lines += [f'### 任务 {index}', _block({'任务标识': task.task_id, '任务状态': task.status})]
        if not task.messages:
            lines.append('该任务过滤后无有效业务交互。')
        for number, message in enumerate(task.messages, 1):
            role = {'user': '用户', 'assistant': '助手', 'tool': '工具反馈'}.get(message.get('role'), '交互')
            lines.append(f'#### 交互 {number} · {role}')
            if 'content' in message and message['content'] is not None:
                lines += ['内容：', _payload(message['content']) if message.get('role') == 'tool' else _block(message['content'])]
            if 'tool_call_id' in message:
                lines += ['对应调用标识：', _block(message['tool_call_id'])]
            for call in message.get('tool_calls', []):
                function = call.get('function', {})
                lines += ['工具调用：', _block({'调用标识': call.get('id'), '工具名称': function.get('name')}),
                          '调用参数：', _payload(function.get('arguments'))]
                # 保留非标准字段，避免渲染时静默丢失有效历史信息。
                extra = {k: v for k, v in call.items() if k not in {'id', 'function'}}
                extra_function = {k: v for k, v in function.items() if k not in {'name', 'arguments'}}
                if extra_function:
                    extra['function'] = extra_function
                if extra:
                    lines += ['调用附加信息：', _block(extra)]
            extra = {k: v for k, v in message.items() if k not in {'role', 'content', 'tool_calls', 'tool_call_id'}}
            if extra:
                lines += ['交互附加信息：', _block(extra)]
    lines += ['## 本次提取主题', '主题名称（topic name）：', _block(request.topic.title),
              '提取指导（scope）：', _block(request.topic.scope)]
    return '\n\n'.join(lines)
