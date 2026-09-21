"""已接受的原生工具交互；不从公开摘要重建参数，不接收私有推理。"""
from copy import deepcopy
import json


def validate_native_messages(messages: list[dict]) -> list[dict]:
    """校验完整的单工具调用/反馈配对，允许配对之间的可信系统提示。

    调用方必须已完成工具 Schema 和业务校验；临时失败及纠错不写入本列表。
    不允许待完成调用持久化为已接受交互，也不替代私有审计。
    """
    if not isinstance(messages, list):
        raise ValueError('原生消息必须为列表')
    result = deepcopy(messages)
    pending = None
    ids = set()
    for message in result:
        if not isinstance(message, dict):
            raise ValueError('原生消息必须为对象')
        role = message.get('role')
        if role == 'assistant':
            if pending is not None or set(message) - {'role', 'content', 'tool_calls'}:
                raise ValueError('助手消息包含未完成配对或未允许字段')
            if message.get('content') not in (None, ''):
                raise ValueError('已接受工具消息不包含普通文本或推理草稿')
            calls = message.get('tool_calls')
            if not isinstance(calls, list) or len(calls) != 1:
                raise ValueError('每条助手消息必须恰好包含一个工具调用')
            call = calls[0]
            if not isinstance(call, dict) or set(call) != {'id', 'type', 'function'} or call['type'] != 'function':
                raise ValueError('工具调用结构不合法')
            identifier = call['id']
            function = call['function']
            if not isinstance(identifier, str) or not identifier.strip() or identifier in ids:
                raise ValueError('调用标识为空或重复')
            if (not isinstance(function, dict) or set(function) != {'name', 'arguments'}
                    or not isinstance(function['name'], str) or not function['name'].strip()
                    or not isinstance(function['arguments'], str)):
                raise ValueError('工具名称或参数不合法')
            def pairs(values):
                data = {}
                for key, value in values:
                    if key in data:
                        raise ValueError('工具参数包含重复键')
                    data[key] = value
                return data
            def reject_constant(value):
                raise ValueError('工具参数不允许非标准数值')
            arguments = json.loads(function['arguments'], object_pairs_hook=pairs, parse_constant=reject_constant)
            if not isinstance(arguments, dict):
                raise ValueError('工具参数必须为 JSON 对象')
            ids.add(identifier)
            pending = identifier
        elif role == 'tool':
            if (set(message) != {'role', 'tool_call_id', 'content'} or pending is None
                    or message['tool_call_id'] != pending or not isinstance(message['content'], str)):
                raise ValueError('工具反馈必须匹配上一条调用且包含文本结果')
            pending = None
        elif role == 'user':
            if (pending is not None or set(message) != {'role', 'source', 'content'}
                    or message['source'] not in {'fifo', 'workspace', 'tool'}
                    or not isinstance(message['content'], str) or not message['content'].strip()):
                raise ValueError('用户角色在轨迹中仅接受有来源的独立系统提示')
        else:
            raise ValueError('原生轨迹包含不允许的角色')
    if pending is not None:
        raise ValueError('工具调用尚未得到反馈，不能提交为已接受轨迹')
    return result


def close_native_messages(messages: list[dict]) -> list[dict]:
    """终态只保留有效调用/反馈；先验证配对和来源，不能用清理掩盖非法轨迹。

    此列表中的 user 已限定为程序操作提示；不按正文标签匹配，避免误删
    工具结果里引用的同名文本。返回副本，终态验收失败时不改变活动任务。
    """
    return [message for message in validate_native_messages(messages) if message['role'] != 'user']


def native_trace_entries(messages: list[dict], *, has_final_output: bool) -> list[dict]:
    """任务封闭后转换成可读轨迹；最终输出正文由独立区域展示一次。"""
    entries = []
    skip_feedback = False
    for message in close_native_messages(messages):
        role = message['role']
        if role == 'assistant':
            call = message['tool_calls'][0]
            function = call['function']
            skip_feedback = has_final_output and function['name'] == 'finish_task'
            if not skip_feedback:
                entries.append({'kind': 'tool_call', 'call_id': call['id'], 'name': function['name'],
                                'content': function['arguments']})
        elif role == 'tool':
            if not skip_feedback:
                entries.append({'kind': 'tool_result', 'call_id': message['tool_call_id'], 'content': message['content']})
            skip_feedback = False
    return entries
