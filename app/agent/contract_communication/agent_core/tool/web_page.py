"""网页首次打开、精炼结果驻留和翻页；每个会话独立实例。"""
import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass
import hashlib
import hmac
import logging
import re
from typing import Annotated
import secrets
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from app.schema.agent_tool_content import FoldableToolContent, ToolPageReference
from ..subgraph.fifo_management.schema import FIFOExecutionResult
from ..subgraph.web_page import build_web_page_subgraph, WebPageResult
from .registry import RegisteredTool
from .progress import ToolProgress

logger = logging.getLogger(__name__)


class OpenWebPageArguments(BaseModel):
    """当你需要阅读搜索候选网页并获取与当前问题相关的内容时，使用这个工具。首次提供来源标识和关注重点，返回精炼正文第一页；继续阅读同一重点时省略关注重点和页码即可翻到下一页。换关注重点会重新读取并精炼。内容可能不完整，应保留其中的条件与限制，不能将搜索摘要当作已核实正文。"""
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    source_id: str = Field(min_length=1, description='网页搜索列表返回的完整source_id，原样复制，不能使用行号、结果集ID或自行编写URL代替。')
    focus: str | None = Field(default=None, min_length=1, max_length=4000, description='希望从本页获取的内容，用自然语言明确目标对象和关注事项，例如“主商品的配置、价格及适用条件，排除推荐商品”。首次打开或缓存失效后必填；后续省略或null沿用该来源最近查看的驻留重点。指定不同重点会重新精炼；不要填写回答格式指令。')
    keywords: list[Annotated[str, Field(min_length=1, max_length=24)]] = Field(default_factory=list, max_length=5, description='供用户查看当前动作的1至5个简短关键词，例如["处理器","内存","价格"]，从本次关注重点概括，不写完整句子、网址或来源ID。仅用于状态展示，不参与提取或缓存；省略时使用关注重点。')
    page: int | None = Field(default=None, strict=True, description='从1开始的页码。省略或null时，新结果返回第一页，驻留结果返回下一页；指定页码直接读取该页。末尾或非法页不会移动游标。')


@dataclass
class _Snapshot:
    resource_id: str
    source_id: str
    title: str
    focus: str
    result: WebPageResult
    pages: tuple[str, ...]
    page: int = 0


def render_web_page(snapshot, page):
    return '\n'.join(['# 网页精炼内容', '', f'标题：{snapshot.title}',
        f'来源标识：{snapshot.source_id}', f'链接：{snapshot.result.final_url or snapshot.result.url}',
        f'抓取时间：{snapshot.result.fetched_at or "未记录"}', f'关注重点：{snapshot.focus}',
        f'第 {page} / {len(snapshot.pages)} 页', '', snapshot.pages[page - 1], '',
        f'可继续查看第 {page + 1} 页。' if page < len(snapshot.pages) else '已到最后一页。'])


def _failure(code, message):
    return FIFOExecutionResult(status='failed', tool_result={'code': code, 'message': message})


class WebPageViewer:
    def __init__(self, *, sources, settings, authorize, capacity=10, page_chars=3000, graph_factory=None):
        if type(capacity) is not int or capacity < 1 or type(page_chars) is not int or page_chars < 1:
            raise ValueError('网页缓存容量和每页字符数必须为正整数')
        self.sources, self.settings, self.authorize = sources, settings, authorize
        self.capacity, self.page_chars = capacity, page_chars
        self._graph_factory = graph_factory or build_web_page_subgraph
        self._items = OrderedDict()
        self._key = secrets.token_bytes(32)
        self._lock = asyncio.Lock()
        self.closed = False
        # 每次调用的失败/成功模型原始响应独立保留；审计不注入上下文，随会话释放。
        self.audit = deque(maxlen=capacity)

    def close(self):
        self.closed = True
        self._items.clear()
        self.audit.clear()
        self._key = secrets.token_bytes(32)

    async def _check_access(self):
        await self.authorize()
        if self.closed:
            raise PermissionError('会话已释放')

    def _sign(self, resource, locator, nonce):
        return hmac.new(self._key, f'{resource}\n{locator}\n{nonce}'.encode(), hashlib.sha256).hexdigest()

    async def open(self, source_id, focus=None, page=None):
        async with self._lock:
            try:
                await self._check_access()
                if page is not None and (type(page) is not int or page < 1):
                    return _failure('invalid_page', '页码必须为从1开始的整数。')
                if focus is None:
                    key = next((key for key in reversed(self._items) if key[0] == source_id), None)
                    if key is None:
                        return _failure('focus_required', '首次打开或正文缓存已释放，请提供关注重点focus后重新读取。')
                else:
                    key = (source_id, focus)
                snapshot = self._items.get(key)
                if snapshot is None:
                    try:
                        source = self.sources.source(source_id)
                    except ValueError:
                        return _failure('source_expired', '网页来源已失效，请重新搜索并取得来源标识。')
                    records = []
                    self.audit.append(records)
                    graph = self._graph_factory(settings=self.settings, audit=records)
                    output = await graph.ainvoke({'request': {'url': source.url, 'focus': focus}})
                    await self._check_access()  # 驱逐期间结束的网络/模型请求不得重新写入缓存。
                    result = WebPageResult.model_validate(output['result'])
                    if result.status == 'error':
                        return _failure(result.error_code, result.hint)
                    # 只分页最终精炼文本，片段顺序不变，拼接可恢复完整结果。
                    content = result.content
                    pages = tuple(content[i:i+self.page_chars] for i in range(0, len(content), self.page_chars))
                    snapshot = _Snapshot('web-page:' + str(uuid4()), source_id, source.title, focus, result, pages)
                    self._items[key] = snapshot
                    while len(self._items) > self.capacity:
                        self._items.popitem(last=False)
                target = snapshot.page + 1 if page is None else page
                if target > len(snapshot.pages):
                    return _failure('end_of_results' if page is None else 'invalid_page',
                        f'已到最后一页，当前第{snapshot.page}/{len(snapshot.pages)}页。' if page is None else
                        f'页码超出范围，可用页码为1至{len(snapshot.pages)}。')
                snapshot.page = target
                self._items.move_to_end(key)
                locator, nonce = f'page:{target}', str(uuid4())
                reference = ToolPageReference(resource_id=snapshot.resource_id, locator=locator,
                    display_id=nonce + '.' + self._sign(snapshot.resource_id, locator, nonce),
                    media_type='text', description=f'网页精炼内容，第{target}/{len(snapshot.pages)}页，来源{source_id}')
                return FIFOExecutionResult(status='succeeded', content=FoldableToolContent(pages=[reference]),
                    tool_result={'source_id': source_id, 'focus': snapshot.focus, 'page': target,
                                 'total_pages': len(snapshot.pages), 'message': '已读取网页精炼内容。'})
            except PermissionError:
                return _failure('session_expired', '会话已释放，请重新发起读取。')
            except Exception:
                logger.exception('网页读取工具执行失败')
                return _failure('web_page_failed', '网页读取暂时失败，请稍后重试，不能据此判断没有相关信息。')

    async def resolve_page(self, reference):
        await self._check_access()
        try:
            nonce, signature = reference.display_id.split('.', 1)
            if reference.media_type != 'text' or not hmac.compare_digest(signature,
                    self._sign(reference.resource_id, reference.locator, nonce)):
                raise ValueError()
            prefix, number = reference.locator.split(':', 1)
            page = int(number)
            if prefix != 'page' or page < 1:
                raise ValueError()
        except (ValueError, AttributeError):
            raise ValueError('网页页面引用无效，请重新打开网页。') from None
        snapshot = next((item for item in self._items.values() if item.resource_id == reference.resource_id), None)
        if snapshot is None:
            text = '网页正文缓存已释放，请使用来源标识和关注重点重新打开。'
        elif page > len(snapshot.pages):
            raise ValueError('网页页码无效，请重新打开网页。')
        else:
            text = render_web_page(snapshot, page)
        return [{'type': 'text', 'text': text}]


def build_open_web_page_registration(viewer):
    async def execute(operation, args):
        return await viewer.open(**args.model_dump(exclude={'keywords'}))
    def progress(args):
        focus = args.focus
        if focus is None:
            key = next((key for key in reversed(viewer._items) if key[0] == args.source_id), None)
            focus = key[1] if key else ''
        terms = args.keywords or re.split(r'[|｜、，,；;\n]+', focus or '')
        # 状态仅展示简短纯文本；不改变原始focus，也不用于缓存键。
        terms = list(dict.fromkeys(' '.join(term.split())[:24] for term in terms if term.strip()))[:5]
        suffix = '：' + ' | '.join(terms) if terms else ''
        return ToolProgress(type='online-search', message='正在读取网页' + suffix)

    return RegisteredTool('open_web_page' , OpenWebPageArguments.__doc__, OpenWebPageArguments, execute,
        return_types=('ordinary', 'foldable'), progress=ToolProgress(type='online-search', message='正在读取网页'), progress_factory=progress)
