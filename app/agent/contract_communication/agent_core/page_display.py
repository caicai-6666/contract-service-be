"""按完整生成轮次消费临时页面；仅保存待展示引用，不持有会话资源模型。"""
from collections.abc import Awaitable, Callable
from copy import deepcopy

from app.schema.agent_tool_content import ToolPageReference
from .context_rendering.page import render_tool_page_messages, render_tool_receipt
from .subgraph.fifo_management.schema import FIFOExecutionResult

PageResolver = Callable[[ToolPageReference], Awaitable[list[dict]]]


class PageDisplayWindow:
    """每个生成循环独立持有；原始页面只在构造当前请求时解析。"""

    def __init__(self, resolver: PageResolver, *, max_rounds: int = 5):
        if not callable(resolver):
            raise TypeError('页面解析器必须可调用')
        if type(max_rounds) is not int or max_rounds < 1:
            raise ValueError('页面保留轮数必须为正整数')
        self._max_rounds = max_rounds
        self._remaining: dict[str, int] = {}
        self._resolver = resolver
        self._pending: dict[str, FIFOExecutionResult] = {}
        self._seen: set[str] = set()

    def register(self, call_id: str, result: FIFOExecutionResult):
        result = FIFOExecutionResult.model_validate(result)
        if result.content.type == 'ordinary':
            return
        if result.status != 'succeeded' or call_id in self._pending:
            raise ValueError('页面展示必须绑定唯一的成功工具调用')
        ids = {page.display_id for page in result.content.pages}
        if ids & self._seen:
            raise ValueError('重新打开页面必须生成新的 display_id，不能重用展示机会')
        self._seen.update(ids)
        self._pending[call_id] = result.model_copy(deep=True)
        self._remaining[call_id] = self._max_rounds

    async def inject(self, messages: list[dict]) -> tuple[list[dict], tuple[str, ...]]:
        """在所属 tool 回执之后注入，不拆开 assistant/tool 配对；构造不消费。"""
        output = []
        displayed = []
        for message in deepcopy(messages):
            call_id = message.get('tool_call_id') if message.get('role') == 'tool' else None
            result = self._pending.get(call_id)
            if result is None:
                output.append(message)
                continue
            if call_id in displayed:
                raise ValueError('页面工具回执在当前上下文中重复')
            pages = {}
            for ref in result.content.pages:
                pages[ref.display_id] = await self._resolver(ref.model_copy(deep=True))
            rendered = render_tool_page_messages(result, visible_pages=pages, remaining_rounds=self._remaining[call_id])
            message['content'] = render_tool_receipt(result, visible=True)
            output.extend([message, *rendered])
            displayed.append(call_id)
        if set(displayed) != set(self._pending):
            raise ValueError('待展示页面缺少原始工具回执，不能错位注入或默默丢失')
        return output, tuple(displayed)

    def consume(self, displayed: tuple[str, ...], *, finish_reason: str | None):
        # 完整响应即消费，即使其工具协议错误；截断、网络失败不消费。
        if finish_reason in ('stop', 'tool_calls'):
            for call_id in set(displayed):
                if call_id not in self._remaining:
                    continue
                self._remaining[call_id] -= 1
                if self._remaining[call_id] == 0:
                    self._pending.pop(call_id, None)
                    self._remaining.pop(call_id, None)

    def close(self):
        """任务退出仅释放展示引用，不关闭解析器持有的会话资源。"""
        self._pending.clear()
        self._remaining.clear()
        self._seen.clear()
