"""网页搜索与会话内 LRU 结果池；搜索摘要不等同于网页正文。"""
import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import re
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from app.schema.agent_tool_content import FoldableToolContent, ToolPageReference
from ..subgraph.fifo_management.schema import FIFOExecutionResult
from .registry import RegisteredTool
from .progress import ToolProgress

logger = logging.getLogger(__name__)


class SearchWebArguments(BaseModel):
    """当你需要从公开网络寻找资料、事实依据或候选网页时，使用这个工具。将需求组织成搜索关键词，获得结果集ID和条数后，调用view_web_search_results查看候选；搜索摘要不代表已读取或核实网页正文。"""
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=1000, description='搜索关键词或简洁检索短语，组合检索对象、关键条件及必要的时间或地域，例如“上海 企业采购合同 电子签章 效力”。保留实体名称和限定条件，避免寒暄、冗长背景和答案格式要求；不要写成让搜索引擎回答的长篇指令。')


class ViewWebSearchResultsArguments(BaseModel):
    """当你需要选择可打开的网页来源时，使用这个工具查看搜索候选。按搜索引擎返回的相关性顺序展示标题、来源标识、链接和搜索摘要；省略页码首次查看第一页，后续查看下一页。"""
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    result_id: str = Field(min_length=1, description='search_web返回的完整结果集ID，必须原样复制，不得缩写或猜测。')
    page: int | None = Field(default=None, strict=True, description='从1开始的页码；省略或null首次查看第一页，之后查看下一页。指定非法页或到达末尾不会移动当前位置。')


@dataclass(frozen=True)
class WebSource:
    source_id: str
    title: str
    url: str
    snippet: str


@dataclass
class WebSearchSnapshot:
    query: str
    created_at: str
    sources: tuple[WebSource, ...]
    page: int = 0


def render_web_search_results(result_id, snapshot, page, page_size):
    total = len(snapshot.sources)
    pages = (total + page_size - 1) // page_size
    lines = ['# 网页搜索结果', '', f'结果集：{result_id}', f'检索词：{snapshot.query}',
             f'检索时间：{snapshot.created_at}', f'第 {page} / {pages} 页 · 共 {total} 条',
             '按搜索引擎返回的相关性顺序排列；以下为搜索摘要，尚未读取或核实网页正文。']
    start = (page - 1) * page_size
    for index, source in enumerate(snapshot.sources[start:start + page_size], start + 1):
        lines += ['', f'## {index}. {source.title}', f'来源标识：{source.source_id}',
                  f'链接：{source.url}', f'搜索摘要：{source.snippet or "未提供摘要"}']
    lines += ['', f'可继续查看第 {page + 1} 页。' if page < pages else '已到最后一页。']
    return '\n'.join(lines)


def _failure(code, message):
    return FIFOExecutionResult(status='failed', tool_result={'code': code, 'message': message})


class WebSearchPool:
    """每个会话独立实例；来源映射随所属结果集一起驱逐，不产生悬空映射。"""
    def __init__(self, *, capacity=10, page_size=5):
        if type(capacity) is not int or capacity < 1 or type(page_size) is not int or page_size < 1:
            raise ValueError('容量和每页条数必须为正整数')
        self.capacity, self.page_size = capacity, page_size
        self._items = OrderedDict()
        self._references = {}
        self.closed = False

    def close(self):
        self.closed = True
        self._items.clear()
        self._references.clear()

    def put(self, query, rows):
        if self.closed:
            raise ValueError('会话已释放，请重新发起搜索。')
        sources, seen = [], set()
        for row in rows:
            url = str(row.get('href') or row.get('url') or '').strip()
            try:
                parts = urlsplit(url)
                if parts.scheme.lower() not in ('http', 'https') or not parts.hostname:
                    continue
                # 仅去掉片段并规范化协议/主机；保留查询参数，避免误合并不同正文。
                key = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ''))
            except ValueError:
                continue
            if key in seen:
                continue
            seen.add(key)
            sources.append(WebSource('web-source:' + str(uuid4()),
                ' '.join(str(row.get('title') or url).split()), url,
                ' '.join(str(row.get('body') or '').split())))
        if not sources:
            return None, 0
        result_id = 'web-search:' + str(uuid4())
        self._items[result_id] = WebSearchSnapshot(query, datetime.now(timezone.utc).isoformat(), tuple(sources))
        while len(self._items) > self.capacity:
            expired, _ = self._items.popitem(last=False)
            self._references = {k: v for k, v in self._references.items() if v[0] != expired}
        return result_id, len(sources)

    def source(self, source_id):
        for result_id, snapshot in self._items.items():
            for source in snapshot.sources:
                if source.source_id == source_id:
                    self._items.move_to_end(result_id)
                    return source
        raise ValueError('网页来源已失效，请重新搜索。')

    async def view(self, result_id, page=None):
        snapshot = self._items.get(result_id)
        if snapshot is None:
            return _failure('result_expired', '搜索结果已释放或不存在，请重新搜索。')
        pages = (len(snapshot.sources) + self.page_size - 1) // self.page_size
        target = snapshot.page + 1 if page is None else page
        if type(target) is not int or target < 1 or target > pages:
            return _failure('end_of_results' if page is None else 'invalid_page',
                f'已到最后一页，当前第{snapshot.page}/{pages}页。' if page is None else f'页码非法，可用页码为1至{pages}。')
        snapshot.page = target
        self._items.move_to_end(result_id)
        # 每个资源页固定引用，反复翻页不累计无界的引用记录。
        key = (result_id, target)
        display = next((k for k, v in self._references.items() if v == key), None) or str(uuid4())
        self._references[display] = key
        ref = ToolPageReference(resource_id=result_id, locator=f'page:{target}', display_id=display,
            media_type='text', description=f'网页搜索结果第{target}/{pages}页，共{len(snapshot.sources)}条')
        return FIFOExecutionResult(status='succeeded', content=FoldableToolContent(pages=[ref]),
            tool_result={'result_id': result_id, 'page': target, 'total_pages': pages, 'count': len(snapshot.sources)})

    async def resolve_page(self, reference):
        if reference.resource_id not in self._items:
            return [{'type': 'text', 'text': '搜索结果已释放或不存在，请重新搜索。'}]
        key = self._references.get(reference.display_id)
        if key is None or reference.locator != f'page:{key[1]}' or key[0] != reference.resource_id or reference.media_type != 'text':
            raise ValueError('网页搜索页面引用无效，请重新查看。')
        snapshot = self._items.get(reference.resource_id)
        text = ('搜索结果已释放，请重新搜索。' if snapshot is None else
                render_web_search_results(reference.resource_id, snapshot, key[1], self.page_size))
        return [{'type': 'text', 'text': text}]


class WebSearchService:
    """共用并发额度；取消调用不会提前释放仍在运行的同步搜索额度。"""
    def __init__(self, *, timeout=10, max_results=20, concurrency=2, searcher=None):
        self.timeout, self.max_results = timeout, max_results
        self._semaphore = asyncio.Semaphore(concurrency)
        self._searcher = searcher or self._search

    def _search(self, query):
        from ddgs import DDGS
        return DDGS(timeout=self.timeout).text(query, max_results=self.max_results)

    async def search(self, query):
        await self._semaphore.acquire()
        task = asyncio.create_task(asyncio.to_thread(self._searcher, query))
        def done(future):
            self._semaphore.release()
            if not future.cancelled():
                future.exception()  # 调用方取消后仍回收后台异常。
        task.add_done_callback(done)
        return await asyncio.shield(task)


def build_web_search_registrations(*, pool, service, authorize):
    async def search(operation, args):
        await authorize()
        try:
            rows = await service.search(args.query)
            await authorize()
            result_id, count = pool.put(args.query, rows)
        except PermissionError:
            return _failure('session_expired', '会话已释放，请重新发起搜索。')
        except Exception:
            logger.exception('网页搜索失败')
            return _failure('search_failed', '网页搜索服务暂时不可用，请稍后重试；不能据此判断没有结果。')
        payload = {'count': count}
        if result_id:
            payload['result_id'] = result_id
        else:
            payload['message'] = '未找到符合条件的网页，请调整检索词。'
        return FIFOExecutionResult(status='succeeded', tool_result=payload)

    async def view(operation, args):
        await authorize()
        return await pool.view(**args.model_dump())

    def search_progress(args):
        # 只格式化展示，保留实际交给搜索引擎的完整 query。
        keywords = [part for part in re.split(r'[\s|｜、，,；;]+', args.query) if part]
        text = ' | '.join(keywords) or args.query
        prefix = '正在搜索网页：'
        limit = 2000 - len(prefix)
        if len(text) > limit:
            text = text[:limit - 1] + '…'
        return ToolProgress(type='online-search', message=prefix + text)

    return (
        RegisteredTool('search_web', SearchWebArguments.__doc__, SearchWebArguments, search,
            progress=ToolProgress(type='online-search', message='正在搜索网页'),
            progress_factory=search_progress),
        RegisteredTool('view_web_search_results', ViewWebSearchResultsArguments.__doc__, ViewWebSearchResultsArguments,
            view, return_types=('ordinary', 'foldable'),
            progress=ToolProgress(type='online-search', message='正在查看搜索结果')),
    )
