"""重组 OpenAI 工具增量；展示回调不改变完整响应的后续校验契约。"""
from copy import deepcopy
from openai.types.chat import ChatCompletion


async def collect_tool_stream(stream, on_delta):
    message = {'role': 'assistant', 'content': None}
    calls = {}
    response_id, model, created, usage, finish = '', '', 0, None, None
    try:
        async for chunk in stream:
            response_id, model, created = chunk.id, chunk.model, chunk.created
            if chunk.usage is not None:
                usage = chunk.usage.model_dump()
            for choice in chunk.choices:
                if choice.index != 0:
                    raise ValueError('工具流只允许一个候选响应')
                delta = choice.delta.model_dump(exclude_none=True)
                for key in ('content', 'reasoning', 'reasoning_content', 'refusal'):
                    if key in delta:
                        message[key] = (message.get(key) or '') + delta[key]
                for part in delta.get('tool_calls', ()):
                    index = part['index']
                    call = calls.setdefault(index, {'id': '', 'type': 'function',
                                                    'function': {'name': '', 'arguments': ''}})
                    if part.get('id'):
                        call['id'] += part['id']
                    if part.get('type'):
                        call['type'] = part['type']
                    for key in ('name', 'arguments'):
                        call['function'][key] += part.get('function', {}).get(key) or ''
                if calls:
                    message['tool_calls'] = [calls[i] for i in sorted(calls)]
                if choice.finish_reason is not None:
                    finish = choice.finish_reason
                # 私有推理只进入最终响应审计，不传给展示回调。
                await on_delta(deepcopy({k: v for k, v in message.items()
                                         if k not in ('reasoning', 'reasoning_content')}))
    finally:
        await stream.close()
    # 缺少正常结束标记时按截断处理，禁止将半截调用当作成功动作。
    return ChatCompletion.model_validate({'id': response_id, 'model': model, 'created': created,
        'object': 'chat.completion', 'usage': usage,
        'choices': [{'index': 0, 'message': message, 'finish_reason': finish or 'length'}]})
