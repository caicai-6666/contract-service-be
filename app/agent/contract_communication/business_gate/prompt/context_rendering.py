"""上下文相关性的白名单投影；只渲染已选历史，不访问数据库或修改原轨迹。"""

from collections.abc import Sequence

import yaml

from app.schema.communication import ConversationHistoryRecord
from ..state import FileSummary


_STATUS_LABELS = {
    'completed': '正常完成', 'superseded': '用户调整方向',
    'cancelled': '用户手动终止', 'failed': '执行失败',
}
_FILE_AVAILABILITY = {
    'accepted': '已准入，实际读取仍需校验', 'unavailable': '不可用',
    'pending': '尚未确认', None: '未提供可用性信息',
}


class _ReadableDumper(yaml.SafeDumper):
    """多行原文采用缩进块，不能通过伪造标题或分隔线逃出当前字段。"""


def _represent_text(dumper, value):
    return dumper.represent_scalar('tag:yaml.org,2002:str', value, style='|' if '\n' in value else None)


_ReadableDumper.add_representer(str, _represent_text)


def _yaml(data):
    # 不裁掉尾部换行，否则最后一个多行字段的原文会被改变。
    return yaml.dump(data, Dumper=_ReadableDumper, allow_unicode=True, sort_keys=False, width=1000)


def _text(value, *, missing):
    if value is None:
        return missing
    if not isinstance(value, str):
        raise ValueError('可见文字必须是字符串或 null')
    return value if value.strip() else missing


def _history_files(files):
    if not isinstance(files, list) or any(not isinstance(file, dict) for file in files):
        raise ValueError('历史附件必须为对象列表')
    rows = []
    for number, file in enumerate(files, 1):
        admission = file.get('admission')
        if admission not in _FILE_AVAILABILITY:
            raise ValueError('历史附件准入状态无效')
        rows.append({
            '文件序号': number,
            # 旧数据没有描述时明确缺失，不回退原文件名或通过路径猜测内容。
            '文件名称': _text(file.get('display_name'), missing='未生成名称'),
            '内容摘要': _text(file.get('summary'), missing='未提供摘要'),
            '文件可用性': _FILE_AVAILABILITY[admission],
        })
    return rows or '该轮未上传文件'


def _final_answer(record):
    if record.status not in {'completed', 'superseded'}:
        return None
    trace = record.payload.get('trace', [])
    if not isinstance(trace, list) or any(not isinstance(item, dict) for item in trace):
        raise ValueError('历史轨迹必须为对象列表')
    # trace 的列表顺序就是交互顺序。内部 ID 仅用于拼回消息，不进入模型文本。
    groups = {}
    for item in trace:
        if item.get('type') != 'message':
            continue
        identity = item.get('message_id')
        if not isinstance(identity, str) or not identity:
            raise ValueError('历史消息缺少有效标识')
        groups.setdefault(identity, []).append(item)
    answers = []
    for fragments in groups.values():
        if not any(item.get('message_kind') == 'final' for item in fragments):
            continue
        if any(item.get('message_kind') != 'final' for item in fragments):
            raise ValueError('同一消息的用途不一致')
        # 任一片段中断或未完成时整条省略，不将其中已输出部分当作最终回答。
        if not all(item.get('status') == 'completed' for item in fragments):
            continue
        if any(not isinstance(item.get('text'), str) for item in fragments):
            raise ValueError('最终回答片段必须为文字')
        content = ''.join(item['text'] for item in fragments)
        if content.strip():
            answers.append(content)
    if len(answers) > 1:
        raise ValueError('单轮存在多个完整最终回答，不能静默择取')
    return answers[0] if answers else None


def render_context_relevance_input(
    *, history: Sequence[ConversationHistoryRecord], text: str | None,
    file_summaries: Sequence[FileSummary] = (),
) -> str:
    """渲染调用方已选的 0～5 轮历史；空历史显式占位，不跨摘要边界补取。

    调用方负责归属、最新摘要边界、排除当前轮及近五轮选择。此处再次拒绝摘要、
    rejected 和非适用状态，避免把未过滤的完整缓存误交给模型。
    """
    records = sorted((ConversationHistoryRecord.model_validate(
        row.model_dump() if isinstance(row, ConversationHistoryRecord) else row) for row in history),
        key=lambda row: row.sequence)
    if len(records) > 5:
        raise ValueError('最多提供已选的 5 轮历史；无有效历史时使用空列表')
    if (any(row.kind != 'task' or row.status not in _STATUS_LABELS for row in records)
        or len({row.sequence for row in records}) != len(records)
        or len({row.turn_id for row in records}) != len(records)
        or any(not row.turn_id for row in records)):
        raise ValueError('只能渲染唯一且已筛选的历史任务，不接受摘要、拒绝或非适用状态')
    current_files = sorted((FileSummary.model_validate(file) for file in file_summaries),
                           key=lambda file: file.file_index)
    if [file.file_index for file in current_files] != list(range(len(current_files))):
        raise ValueError('本轮文件摘要必须按完整上传序列提供')
    current_text = _text(text, missing='本轮未提供文字')
    if not current_files and (text is None or not text.strip()):
        raise ValueError('本轮必须有文字或文件')

    sections = ['## 近5轮上下文\n\n以下按从旧到新排列，编号仅对应本次展示范围。']
    if not records:
        sections[0] += '\n\n本次未提供可用历史任务。'
    for number, record in enumerate(records, 1):
        source = record.payload.get('input', {})
        # 兼容早期纯文字 input，但不解析任意 JSON 文本为额外字段。
        if isinstance(source, str):
            source = {'text': source}
        if not isinstance(source, dict):
            raise ValueError('历史用户输入必须为对象或旧版纯文字')
        data = {
            '任务状态': _STATUS_LABELS[record.status],
            '用户文字': _text(source.get('text'), missing='该轮未提供文字'),
            '用户文件': _history_files(source.get('files', [])),
        }
        answer = _final_answer(record)
        if answer is not None:
            data['助手最终回答'] = answer
        sections.append(f'### 历史第{number}轮\n\n' + _yaml(data))
    sections.extend([
        '## 本轮文字\n\n' + _yaml({'用户文字': current_text}),
        '## 本轮文件摘要\n\n' + _yaml({'用户文件': [
            {'文件序号': file.file_index + 1, '文件名称': file.display_name, '内容摘要': file.summary}
            for file in current_files
        ] or '本轮未上传文件'}),
    ])
    return '\n\n---\n\n'.join(sections)
