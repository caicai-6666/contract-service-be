"""网页搜索与会话内 LRU 结果池；搜索摘要不等同于网页正文。"""
import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import re
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from typing import Literal
import httpx
from app.core.config import get_settings
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator
from app.schema.agent_tool_content import FoldableToolContent, ToolPageReference
from ..subgraph.fifo_management.schema import FIFOExecutionResult
from .registry import RegisteredTool
from .progress import ToolProgress

logger = logging.getLogger(__name__)


class SearchModel(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class SearchEntry(SearchModel):
    channel: Literal['general'] = Field(description='网页搜索来源标识，固定 general。')
    rank: int = Field(ge=1, description='供应商返回的原始名次，从1开始。')
    title: str = Field(min_length=1, description='网页标题。')
    content: str = Field(description='网页搜索摘要，不等同于已读取正文。')
    url: HttpUrl = Field(description='可打开的网页来源地址，必须为 HTTP(S) URL。')


class WebSearchResult(SearchModel):
    status: Literal['success', 'error'] = Field(description='成功可为空列表；接口失败不能伪装成成功零结果。')
    entries: list[SearchEntry] = Field(default_factory=list, description='按该搜索供应商原始顺序规范化的结果。')
    hint: str | None = Field(default=None, description='失败原因或结果限制说明。')
    error_code: str | None = Field(default=None, description='失败机器码；成功时为空。')

    @model_validator(mode='after')
    def validate_status(self):
        if self.status == 'error' and (self.entries or not self.hint or not self.error_code):
            raise ValueError('失败搜索必须说明原因，不能携带成功条目')
        if self.status == 'success' and self.error_code is not None:
            raise ValueError('成功搜索不能携带错误码')
        return self


class SearchWebArguments(BaseModel):
    """当你需要从公开网络寻找资料、事实依据或候选网页时，使用这个工具。说明要查的对象及所需资料，获得结果集ID和条数后，调用view_web_search_results查看候选；搜索摘要不代表已读取或核实网页正文。"""
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=1000, description='完整简洁的搜索需求，说明检索对象、所需资料及时间、地域、来源等限制；例如“某公司 官网 最近公告”。保留实体名称和排除条件，不附加答案排版要求。')


class ViewWebSearchResultsArguments(BaseModel):
    """当你需要查看搜索到的资料或选择可打开的网页时，使用这个工具查看搜索候选。展示网页标题、链接和摘要。只有带链接的网页来源可以继续打开；省略页码首次查看第一页，后续查看下一页。"""
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    result_id: str = Field(min_length=1, description='search_web返回的完整结果集ID，必须原样复制，不得缩写或猜测。')
    page: int | None = Field(default=None, strict=True, description='从1开始的页码；省略或null首次查看第一页，之后查看下一页。指定非法页或到达末尾不会移动当前位置。')


@dataclass(frozen=True)
class WebSource:
    source_id: str
    title: str
    url: str | None
    snippet: str
    channel: str = 'general'
    rank: int = 0


@dataclass
class WebSearchSnapshot:
    query: str
    created_at: str
    sources: tuple[WebSource, ...]
    page: int = 0
    status: str = 'success'
    hint: str | None = None


def render_web_search_results(result_id, snapshot, page, page_size):
    total = len(snapshot.sources)
    pages = (total + page_size - 1) // page_size
    lines = ['# 搜索结果', '', f'结果集：{result_id}', f'检索词：{snapshot.query}',
             f'检索时间：{snapshot.created_at}', f'第 {page} / {pages} 页 · 共 {total} 条',
             '网页摘要不等同于已读取正文。']
    start = (page - 1) * page_size
    for index, source in enumerate(snapshot.sources[start:start + page_size], start + 1):
        lines += ['', f'## {index}. {source.title}']
        if source.url:
            lines += [f'来源标识：{source.source_id}', f'链接：{source.url}']
        lines += ['搜索摘要：' +
                  (source.snippet or '未提供详情')]
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
        self.audit = deque(maxlen=capacity)

    def close(self):
        self.closed = True
        self._items.clear()
        self.audit.clear()
        self._references.clear()

    def put(self, query, rows, *, status='success', hint=None):
        if self.closed:
            raise ValueError('会话已释放，请重新发起搜索。')
        sources, seen = [], set()
        for row in rows:
            if isinstance(row, SearchEntry):
                # 服务已规范化网页结果，缓存保留供应商顺序。
                sources.append(WebSource('web-source:' + str(uuid4()),
                    ' '.join(row.title.split()), str(row.url) if row.url else None,
                    row.content, row.channel, row.rank))
                continue
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
        self._items[result_id] = WebSearchSnapshot(query, datetime.now(timezone.utc).isoformat(), tuple(sources), status=status, hint=hint)
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
            media_type='text', description=f'搜索结果第{target}/{pages}页，共{len(snapshot.sources)}条')
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


def _search_error(code: str, hint: str) -> WebSearchResult:
    return WebSearchResult(status='error', error_code=code, hint=hint)


def _parse_web_results(payload: object) -> WebSearchResult:
    """兼容直接对象和 code/data 信封，严格区分空召回与损坏响应。"""
    if not isinstance(payload, dict):
        raise ValueError('搜索响应不是对象')
    code = payload.get('code')
    if code is not None and str(code) != '200':
        return _provider_failure(code)
    body = payload.get('data', payload)
    pages = body.get('webPages') if isinstance(body, dict) else None
    rows = pages.get('value') if isinstance(pages, dict) else None
    if not isinstance(rows, list):
        raise ValueError('缺少网页结果数组')
    entries = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('网页条目不是对象')
        title, url = row.get('name'), row.get('url')
        if not isinstance(title, str) or not title.strip() or not isinstance(url, str) or not url.strip():
            raise ValueError('网页缺少标题或链接')
        summary, snippet = row.get('summary'), row.get('snippet')
        if any(v is not None and not isinstance(v, str) for v in (summary, snippet)):
            raise ValueError('摘要格式无效')
        entries.append(SearchEntry(channel='general', rank=len(entries)+1, title=title,
            url=url, content=(summary or '').strip() or (snippet or '').strip()))
    return WebSearchResult(status='success', entries=entries)


def _provider_failure(code) -> WebSearchResult:
    # 不回显服务商原始消息，避免将敏感请求或不可信正文传给模型。
    mapping = {
        '401': ('bocha_unauthorized', '网页搜索服务鉴权失败，请检查服务配置。'),
        '403': ('bocha_forbidden', '网页搜索服务未获授权，请检查接口权限。'),
        '402': ('bocha_payment_required', '网页搜索服务额度或支付状态异常，请检查账户。'),
        '429': ('bocha_rate_limited', '网页搜索服务请求过于频繁，请稍后重试。'),
    }
    error_code, hint = mapping.get(str(code), ('bocha_provider_error', '网页搜索服务返回错误，请稍后重试。'))
    return WebSearchResult(status='error', error_code=error_code, hint=hint)


class WebSearchService:
    """直接异步请求博查，共享并发额度；无模型路由和子图调度。"""
    def __init__(self, *, settings=None, concurrency=2, transport=None):
        if concurrency < 1:
            raise ValueError('搜索并发数必须为正数')
        self.settings = settings
        self._semaphore = asyncio.Semaphore(concurrency)
        self._transport = transport

    async def search(self, query, *, audit=None) -> WebSearchResult:
        async with self._semaphore:
            return await self._request(query, audit=audit)

    async def _request(self, query, *, audit=None) -> WebSearchResult:
        request = SearchWebArguments(query=query)
        config = (self.settings or get_settings()).bocha
        key = config.api_key.get_secret_value().strip()
        if not key:
            return _search_error('bocha_not_configured', '网页搜索服务尚未配置，请联系管理员。')
        payload = {'query': request.query, 'summary': True,
                   'freshness': 'noLimit', 'count': config.web_search_count}
        event = {'stage': 'general_search', 'request': payload}
        if audit is not None:
            audit.append(event)
        try:
            # 总截止时间覆盖整个请求；禁止重定向，避免鉴权头被转发至其他站点。
            async with asyncio.timeout(config.timeout_seconds):
                async with httpx.AsyncClient(timeout=config.timeout_seconds, trust_env=config.trust_env,
                                             follow_redirects=False, transport=self._transport) as client:
                    response = await client.post(config.base_url + '/v1/web-search', json=payload,
                        headers={'Authorization': 'Bearer ' + key})
            event['http_status'] = response.status_code
            if not response.is_success:
                return _provider_failure(response.status_code)
            event['response'] = response.text.replace(key, '[REDACTED]')
            return _parse_web_results(response.json())
        except (TimeoutError, httpx.TimeoutException):
            return _search_error('bocha_timeout', '网页搜索服务响应超时，请稍后重试。')
        except httpx.RequestError:
            return _search_error('bocha_connection_failed', '网页搜索服务连接失败，请稍后重试。')
        except (ValueError, TypeError):
            return _search_error('bocha_invalid_response', '网页搜索服务返回的数据格式异常，请稍后重试。')


def build_web_search_registrations(*, pool, service, authorize):
    async def search(operation, args):
        await authorize()
        try:
            records = []
            pool.audit.append(records)
            result = await service.search(args.query, audit=records)
            await authorize()
            # 对外隐藏路由与供应商诊断；完整结算状态仅留在会话私有审计。
            records.append({'stage': 'search_outcome', 'status': result.status,
                            'error_code': result.error_code, 'hint': result.hint,
                            'count': len(result.entries)})
            if result.status == 'error':
                return _failure('search_failed', '本次搜索未取得可用结果，请稍后重试或调整搜索需求。')
            result_id, count = pool.put(args.query, result.entries, status=result.status, hint=result.hint)
        except PermissionError:
            return _failure('session_expired', '会话已释放，请重新发起搜索。')
        except Exception:
            logger.exception('网页搜索失败')
            return _failure('search_failed', '本次搜索未取得可用结果，请稍后重试或调整搜索需求。')
        payload = {'count': count}
        if result_id:
            payload['result_id'] = result_id
        else:
            payload['message'] = '未找到符合条件的结果，请调整搜索需求。'
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
