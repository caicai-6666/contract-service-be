"""独立推理 FIFO；绝对序号不随驱逐重排，工具调用标识用于消息定位。"""
from copy import deepcopy
from typing import Literal
from pydantic import Field, model_validator
from .communication_workspace import WorkspaceObject, WorkspaceText


class ReasoningEntry(WorkspaceObject):
    position: int = Field(gt=0, description='会话内单调递增的思考位置，驱逐后不重排。')
    task_id: WorkspaceText = Field(description='所属任务的稳定记录标识。')
    call_id: WorkspaceText = Field(description='原始 assistant 响应的工具调用标识，用于准确回注。')
    fields: dict[Literal['reasoning', 'reasoning_content'], WorkspaceText] = Field(description='原生推理字段，保持服务端字段名与完整文本，不拼成普通正文。')
    tokens: int = Field(ge=0, description='本条完整思考的接口计数。')

    @model_validator(mode='after')
    def nonempty(self):
        if not self.fields:
            raise ValueError('思考条目必须包含原生推理内容')
        return self


class ReasoningWindow(WorkspaceObject):
    version: Literal['reasoning-window-v1'] = 'reasoning-window-v1'
    next_position: int = Field(default=1, gt=0, description='下一个绝对思考位置。')
    entries: list[ReasoningEntry] = Field(default_factory=list, description='按绝对位置升序排列的有限思考窗口。')

    @model_validator(mode='after')
    def ordered(self):
        positions = [entry.position for entry in self.entries]
        anchors = [(entry.task_id, entry.call_id) for entry in self.entries]
        if positions != sorted(set(positions)) or (positions and positions[-1] >= self.next_position):
            raise ValueError('思考绝对位置非法')
        if len(anchors) != len(set(anchors)):
            raise ValueError('思考不能重复绑定同一响应')
        return self

    def append(self, *, task_id, call_id, fields, tokens, max_rounds, max_tokens):
        if type(max_rounds) is not int or max_rounds < 0 or type(max_tokens) is not int or max_tokens < 0:
            raise ValueError('思考窗口限制必须为非负整数')
        entry = ReasoningEntry(position=self.next_position, task_id=task_id, call_id=call_id, fields=fields, tokens=tokens)
        previous = next((item for item in self.entries if (item.task_id,item.call_id)==(task_id,call_id)),None)
        if previous is not None:
            if previous.fields != fields or previous.tokens != tokens:
                raise ValueError('思考响应标识冲突')
            return self.model_copy(deep=True)
        values = [*self.entries, entry]
        while values and (len(values) > max_rounds or sum(item.tokens for item in values) > max_tokens):
            values.pop(0)
        return ReasoningWindow(next_position=self.next_position+1, entries=values)

    def limited(self, *, max_rounds, max_tokens):
        if type(max_rounds) is not int or max_rounds < 0 or type(max_tokens) is not int or max_tokens < 0:
            raise ValueError('思考窗口限制必须为非负整数')
        values = list(self.entries)
        while values and (len(values) > max_rounds or sum(item.tokens for item in values) > max_tokens):
            values.pop(0)
        return ReasoningWindow(next_position=self.next_position, entries=values)

    def inject(self, messages, *, task_id):
        """仅修改匹配的原始 assistant；不使用随 system/guidance 变化的消息下标。"""
        result = deepcopy(messages)
        entries = {entry.call_id: entry for entry in self.entries if entry.task_id == task_id}
        for message in result:
            if message.get('role') != 'assistant':
                continue
            message.pop('reasoning', None)
            message.pop('reasoning_content', None)
            calls = message.get('tool_calls', [])
            if len(calls) == 1 and calls[0].get('id') in entries:
                message.update(entries[calls[0]['id']].fields)
        return result
