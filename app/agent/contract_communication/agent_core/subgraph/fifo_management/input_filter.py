"""摘要输入的单一过滤规则；仅处理副本，不改变 FIFO 计数和审计。"""
from .schema import FIFOTask

_SYSTEM_SOURCES = {'fifo', 'workspace', 'system_guidence', 'system_guidance', 'system', 'tool_guidance', 'recovery'}


def filter_summary_tasks(tasks: list[FIFOTask]) -> list[FIFOTask]:
    """按程序封装的来源过滤，正文包含标签的真实用户内容仍保留。

    source=tool 的 user 消息是工具发出的系统提示；role=tool 的合法
    业务结果不因相同 source 而删除。任务及范围保留，即使过滤后消息为空。
    本函数不通过正文猜测错误调用；已完成任务的失败链应由执行器清除。
    """
    result = []
    for task in tasks:
        copy = FIFOTask.model_validate(task).model_copy(deep=True, update={'rendered_content': None})
        copy.messages[:] = [m for m in copy.messages
                            if m.get('role') in {'user', 'assistant', 'tool'}
                            and m.get('source') not in _SYSTEM_SOURCES
                            and not (m.get('role') == 'user' and m.get('source') == 'tool')]
        for message in copy.messages:
            message.pop('reasoning', None)
            message.pop('reasoning_content', None)
        result.append(copy)
    return result
