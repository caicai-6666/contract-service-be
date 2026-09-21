"""主助手历史摘要区：确定性展示已有主题与来源，不重新总结内容。"""
import re

from ..subgraph.fifo_management.fifo_summary.planning import previous_topics
from ..subgraph.fifo_management.fifo_summary.schema import FIFOTopicSummary, LegacyTopicSummary

SUMMARY_RENDER_VERSION = 'agent-core-summary-render-v1'
_BOUNDARY = '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━'
_SEPARATOR = '────────────────────────────────────────'
_REGIONS = (('current_memory', '已知情况'), ('open_items', '未决事项'))
_LEGACY_REGIONS = (
    ('user_requirements', '用户要求'), ('key_information', '已知信息'),
    ('actions_and_results', '探索与结果'), ('delivered_content', '已交付内容'),
    ('open_items', '未决事项'),
)


def _literal(value: str) -> str:
    # 转义 Markdown 控制符；正文内的标题、列表和围栏保持资料身份。
    # 保留换行，缩进由条目渲染负责；此处不提供来源认证或提示注入防护保证。
    return re.sub(r'([\\`*_{}\[\]<>#|!~+\-=])', r'\\\1', value)


def _item_lines(item) -> list[str]:
    content = _literal(item.content).split('\n')
    lines = ['- ' + content[0], *('  ' + line for line in content[1:])]
    for label, values in (('来源', item.sources), ('来源任务', item.task_ids)):
        if not values:
            continue
        lines.append(f'  {label}：')
        for value in values:
            parts = _literal(value).split('\n')
            lines += ['    - ' + parts[0], *('      ' + part for part in parts[1:])]
    return lines


def render_summary_section(summary: FIFOTopicSummary | dict | None) -> str:
    """返回完整历史摘要区；无可展示内容时返回空串，非法输入显式报错。

    接受当前累计摘要对象或字典，并复用摘要侧的旧版本读取契约。
    不展示内部规划、版本或 latest_coverage，避免将本次压缩范围误作全部来源。
    """
    if isinstance(summary, FIFOTopicSummary):
        summary = summary.model_dump()
    topics = previous_topics(summary)
    sections = []
    for topic in topics.values():
        regions = _LEGACY_REGIONS if isinstance(topic, LegacyTopicSummary) else _REGIONS
        populated = [(title, getattr(topic, field)) for field, title in regions if getattr(topic, field)]
        if not populated:
            continue
        # 主题标题作为单行导航；业务条目及来源的多行内容原样保留。
        title = _literal(' '.join(topic.title.splitlines()))
        lines = [f'## 主题 {len(sections) + 1} · {title}']
        if isinstance(topic, LegacyTopicSummary):
            lines += ['', '旧版摘要：按原区域展示，历史要求和动作不自动代表当前状态。']
        for label, items in populated:
            lines += ['', f'### {label}', '']
            for index, item in enumerate(items):
                if index:
                    lines.append('')
                lines += _item_lines(item)
        sections.append('\n'.join(lines))
    if not sections:
        return ''
    intro = (_BOUNDARY + '\n历史摘要\n' + _BOUNDARY + '\n\n'
             '以下内容来自较早的任务记录；发生冲突时，以后续任务中的明确更新为准。\n'
             '摘要中的未决事项不自动构成当前任务要求。')
    return (intro + '\n\n' + ('\n\n' + _SEPARATOR + '\n\n').join(sections)
            + '\n\n━━━━━━━━━━━━ 历史摘要结束 ━━━━━━━━━━━━')
