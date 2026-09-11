"""公开轨迹投影：只消费已校验输出，不从进度文案推测工具或记录私有推理。"""

from copy import deepcopy
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from app.schema.communication import (
    CommunicationModel, MessageDeltaData, MessageCompletedData, TERMINAL_STATUSES,
)


class TraceReference(CommunicationModel):
    type: Literal['contract', 'web']
    location: str = Field(min_length=1, max_length=4096)

    @model_validator(mode='after')
    def validate_location(self):
        if self.type == 'web':
            parsed = urlsplit(self.location)
            if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
                raise ValueError('网页引用必须是 HTTP/HTTPS 地址')
        elif not self.location.startswith('/') or '..' in self.location or '\\' in self.location:
            raise ValueError('合同引用必须为安全的文件保存地址')
        return self


class ToolCallTrace(CommunicationModel):
    type: Literal['tool_call'] = 'tool_call'
    call_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=2000)
    input_summary: str = Field(max_length=4000)


class ToolResultTrace(CommunicationModel):
    type: Literal['tool_result'] = 'tool_result'
    call_id: str = Field(min_length=1, max_length=128)
    status: Literal['succeeded', 'failed', 'interrupted']
    output_summary: str | None = Field(default=None, max_length=8000)
    references: tuple[TraceReference, ...] = ()


def append_tool(payload: dict, item: ToolCallTrace | ToolResultTrace) -> dict:
    result = deepcopy(payload)
    trace = result['trace']
    calls = {r['call_id'] for r in trace if r['type'] == 'tool_call'}
    results = {r['call_id'] for r in trace if r['type'] == 'tool_result'}
    if isinstance(item, ToolCallTrace) and item.call_id in calls:
        raise ValueError('工具调用标识不能重复')
    if isinstance(item, ToolResultTrace) and (item.call_id not in calls or item.call_id in results):
        raise ValueError('工具结果必须对应尚未结束的调用')
    trace.append({'sequence': len(trace) + 1, **item.model_dump(mode='json')})
    return result


def project_event(payload: dict, data, snapshot) -> dict:
    result = deepcopy(payload)
    trace = result['trace']
    if isinstance(data, (MessageDeltaData, MessageCompletedData)):
        fragments = [r for r in trace if r['type'] == 'message' and r['message_id'] == data.message_id]
        if isinstance(data, MessageDeltaData):
            if trace and trace[-1]['type'] == 'message' and trace[-1]['message_id'] == data.message_id:
                trace[-1]['text'] += data.delta
            else:
                trace.append(dict(sequence=len(trace) + 1, type='message', message_id=data.message_id,
                                  message_kind=data.message_kind, text=data.delta, status='streaming', references=[]))
        else:
            # 旧 SSE 引用是正式合同 SHA-256 ID，按已确定的文件地址规则转换，禁止编造地址。
            references = []
            for reference in data.references:
                if not re.fullmatch('[0-9a-f]{64}', reference.document_id):
                    raise ValueError('历史引用需要正式合同 SHA-256 标识')
                value = {'type': 'contract', 'location': f'/{reference.document_id}.pdf'}
                if value not in references:
                    references.append(value)
            if not fragments:
                trace.append(dict(sequence=len(trace) + 1, type='message', message_id=data.message_id,
                                  message_kind=data.message_kind, text=data.text, status=data.status, references=references))
            for fragment in fragments:
                fragment.update(status=data.status, references=references)
    if snapshot.status in TERMINAL_STATUSES:
        for item in trace:
            if item['type'] == 'message' and item['status'] == 'streaming':
                item['status'] = 'interrupted'
        finished = {r['call_id'] for r in trace if r['type'] == 'tool_result'}
        for call in tuple(trace):
            if call['type'] == 'tool_call' and call['call_id'] not in finished:
                trace.append(dict(sequence=len(trace) + 1, type='tool_result', call_id=call['call_id'],
                                  status='interrupted', output_summary=None, references=[]))
    return result
