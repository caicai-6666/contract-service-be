"""两种用户输出工具的临时流式展示；成功提交仍由 FIFO 执行器决定。"""
import json
import re
from uuid import uuid4

from app.schema.communication import MessageCompletedData, MessageDeltaData


_PREFIX = re.compile(r'^\s*\{\s*"content"\s*:\s*"')


def content_prefix(arguments):
    """只解码 content 字符串已完整到达的字符；不猜补转义或 JSON 结构。"""
    match = _PREFIX.match(arguments)
    if match is None:
        return ''
    result, index = [], match.end()
    while index < len(arguments):
        char = arguments[index]
        if char == '"':
            break
        if char == '\\':
            if index + 1 >= len(arguments):
                break
            size = 6 if arguments[index + 1] == 'u' else 2
            if index + size > len(arguments):
                break
            encoded = arguments[index:index + size]
            try:
                value = json.loads('"' + encoded + '"')
                if len(value) == 1 and 0xD800 <= ord(value) <= 0xDBFF:
                    # UTF-16 代理对可能跨 chunk；完整收到低位代理后再发出。
                    if index + 12 > len(arguments):
                        break
                    size = 12
                    value = json.loads('"' + arguments[index:index + size] + '"')
                if any(0xD800 <= ord(c) <= 0xDFFF for c in value):
                    break
            except (ValueError, TypeError):
                break
            result.append(value)
            index += size
        elif ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF:
            break
        else:
            result.append(char)
            index += 1
    return ''.join(result)


class InteractionOutputStream:
    """每次模型响应独立展示 ID；失败预览标记 interrupted，不冒充成功工具结果。"""
    def __init__(self, service, conversation_id, turn_id, owner):
        self.service, self.conversation_id = service, conversation_id
        self.turn_id, self.owner = turn_id, owner
        self.message_id = str(uuid4())
        self.call_id = self.name = self.kind = None
        self.text = ''
        self.active = False
        self.blocked = False

    async def publish(self, data):
        await self.service.publish(self.conversation_id, self.turn_id, owner=self.owner, data=data)

    async def on_delta(self, message):
        if self.blocked:
            return
        calls = message.get('tool_calls', ())
        if len(calls) > 1 or message.get('content') or message.get('refusal'):
            await self.interrupt()
            return
        if not calls:
            return
        call = calls[0]
        name = call['function']['name']
        if self.name is not None and (name != self.name or call['id'] != self.call_id):
            await self.interrupt()
            return
        if name not in ('emit_progress', 'finish_task') or not call['id']:
            return
        self.call_id, self.name = call['id'], name
        self.kind = 'final' if name == 'finish_task' else 'intermediate'
        text = content_prefix(call['function']['arguments'])
        if not text.startswith(self.text):
            await self.interrupt()
            return
        await self.extend(text)

    async def extend(self, text):
        # 控制单事件大小，保持字符边界；不通过 sleep 人为模拟流式生成。
        while len(self.text) < len(text):
            delta = text[len(self.text):len(self.text) + 512]
            await self.publish(MessageDeltaData(message_id=self.message_id,
                message_kind=self.kind, delta=delta))
            self.text += delta
            self.active = True

    async def interrupt(self):
        if self.active:
            await self.service.interrupt_output(self.conversation_id, self.turn_id,
                owner=self.owner, message_id=self.message_id)
            self.active = False
        self.blocked = True

    async def complete(self, output):
        # 只有完整工具调用通过协议、参数及 FIFO 管理校验才允许完成展示。
        if self.active and (self.call_id != output.call_id or self.kind != output.kind
                            or not output.content.startswith(self.text)):
            await self.interrupt()
        if self.blocked or self.call_id != output.call_id:
            self.message_id = str(uuid4())
            self.text = ''
        self.kind = output.kind
        await self.extend(output.content)
        await self.publish(MessageCompletedData(message_id=self.message_id,
            message_kind=output.kind, text=output.content))
        self.active = False
        return self.message_id
