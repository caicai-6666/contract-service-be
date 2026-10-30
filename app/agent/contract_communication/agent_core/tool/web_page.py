"""网页首次打开、处理结果驻留和翻页；每个会话独立实例。"""
import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import hmac
import logging
import re
from typing import Annotated
from urllib.parse import urldefrag
import secrets
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from app.schema.agent_tool_content import FoldableToolContent, ToolPageReference, ToolPageUnavailable
from ..subgraph.fifo_management.schema import FIFOExecutionResult, FIFOGuidance
from ..prompt.guidance import build_system_guidence_message
from .execution import ToolExecution
from ..subgraph.web_page import build_web_page_subgraph, WebPageResult
from .registry import RegisteredTool
from .progress import ToolProgress

logger = logging.getLogger(__name__)


class OpenWebPageArguments(BaseModel):
    """当你需要阅读搜索候选网页的正文和相关图片时，使用这个工具。首次提供来源标识和关注重点，返回精炼正文第一页及筛选后的真实图片；图片集中在第一页，重看图片时指定page=1；继续阅读同一重点时省略关注重点和页码即可翻到下一页。换关注重点会重新读取并精炼。图片仅在本任务内存中保留，任务结束后需重新读取；未看过的图像不能凭图注猜测。没有可用正文时正常返回独立的网页读取提示和no_content，不生成正文页面；提示不是来源原文或事实证据，可换来源或说明资料限制，不推断信息不存在。内容可能不完整，应保留其中的条件与限制，不能将搜索摘要当作已核实正文。"""
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
    task_scope: str | None = None


def render_web_page(snapshot, page):
    return '\n'.join(['# 网页内容', '', f'标题：{snapshot.title}',
        f'来源标识：{snapshot.source_id}', f'链接：{snapshot.result.final_url or snapshot.result.url}',
        f'抓取时间：{snapshot.result.fetched_at or "未记录"}', f'关注重点：{snapshot.focus}',
        f'第 {page} / {len(snapshot.pages)} 页', '', snapshot.pages[page - 1], '',
        snapshot.result.hint or '',
        f'可继续查看第 {page + 1} 页。' if page < len(snapshot.pages) else '已到最后一页。'])


def _failure(code, message):
    return FIFOExecutionResult(status='failed', tool_result={'code': code, 'message': message})


def render_web_page_notice(source, result, count):
    """把读取情况与网页正文显式分隔，限制说明不冒充来源证据。"""
    reason = '\n'.join('> ' + line for line in result.hint.splitlines())
    return '\n'.join([
        '════════════ 网页读取提示 ════════════', '',
        '结果：未获取到可用正文',
        f'当前页面连续无正文：{count} 次',
        '【注意】以下是读取情况说明，不是网页正文，不能作为来源内容引用。', '',
        '网页：' + ' '.join(source.title.split()),
        '链接：' + (result.final_url or result.url), '',
        '原因说明（非正文）：', reason, '',
        '下一步：可以尝试其他来源；若资料仍不足，请向用户说明限制，不推断相关信息不存在。', '',
        '════════════ 提示结束 ════════════'])


class WebPageViewer:
    def __init__(self, *, sources, settings, authorize, capacity=10, page_chars=3000, graph_factory=None):
        if type(capacity) is not int or capacity < 1 or type(page_chars) is not int or page_chars < 1:
            raise ValueError('网页缓存容量和每页字符数必须为正整数')
        self.sources, self.settings, self.authorize = sources, settings, authorize
        self.capacity, self.page_chars = capacity, page_chars
        self._graph_factory = graph_factory or build_web_page_subgraph
        self._items = OrderedDict()
        self._no_content_counts = {}
        self._key = secrets.token_bytes(32)
        self._lock = asyncio.Lock()
        self.closed = False
        self._scope = ContextVar('web_image_task_scope', default=None)
        self._active_scopes = set()
        # 每次调用的失败/成功模型原始响应独立保留；审计不注入上下文，随会话释放。
        self.audit = deque(maxlen=capacity)

    def close(self):
        self.closed = True
        self._items.clear()
        self._no_content_counts.clear()
        self.audit.clear()
        self._key = secrets.token_bytes(32)
        self._active_scopes.clear()

    @contextmanager
    def task_scope(self):
        """每次问答独立持有图片；ContextVar 隔离同树并发，退出时清除该任务全部图像快照。"""
        scope = str(uuid4())
        token = self._scope.set(scope)
        self._active_scopes.add(scope)
        try:
            yield
        finally:
            self._active_scopes.discard(scope)
            for key in list(self._items):
                if self._items[key].task_scope == scope:
                    self._items.pop(key)
            for key in list(self._no_content_counts):
                if key[0] == scope:
                    self._no_content_counts.pop(key)
            self._scope.reset(token)

    async def _check_access(self):
        await self.authorize()
        if self.closed or (self._scope.get() is not None and self._scope.get() not in self._active_scopes):
            raise PermissionError('会话已释放')

    def _sign(self, resource, locator, nonce):
        return hmac.new(self._key, f'{resource}\n{locator}\n{nonce}'.encode(), hashlib.sha256).hexdigest()

    async def open(self, source_id, focus=None, page=None, *, defer_commit=False):
        async with self._lock:
            try:
                await self._check_access()
                scope = self._scope.get()
                if page is not None and (type(page) is not int or page < 1):
                    return _failure('invalid_page', '页码必须为从1开始的整数。')
                if focus is None:
                    key = next((key for key in reversed(self._items) if key[0] == source_id
                                and (len(key) == 2 or key[2] == self._scope.get())), None)
                    if key is None:
                        return _failure('focus_required', '首次打开或原正文已不可读取，请提供关注重点focus后重新读取。')
                else:
                    scoped_key = (source_id, focus, self._scope.get())
                    key = scoped_key if scoped_key in self._items else (source_id, focus)
                snapshot = self._items.get(key)
                if snapshot is None:
                    try:
                        source = self.sources.source(source_id)
                    except ValueError:
                        return _failure('source_expired', '网页来源已失效，请重新搜索并取得来源标识。')
                    if not source.url:
                        return _failure('source_not_openable', '该来源没有可打开的网页链接，请重新搜索。')
                    records = []
                    self.audit.append(records)
                    graph = self._graph_factory(settings=self.settings, audit=records,
                        image_max_candidates=self.settings.communication_web_image_max_candidates,
                        image_max_selected=self.settings.communication_web_image_max_selected,
                        image_max_bytes=self.settings.communication_web_image_max_bytes)
                    output = await graph.ainvoke({'request': {'url': source.url, 'focus': focus}})
                    await self._check_access()  # 驱逐期间结束的网络/模型请求不得重新写入缓存。
                    result = WebPageResult.model_validate(output['result'])
                    if result.status == 'error':
                        return _failure(result.error_code, result.hint)
                    if result.status == 'no_content':
                        # 提示作为完整业务结果提交，不登记空页面，也不推进已有阅读位置。
                        counter_key = (scope, urldefrag(result.final_url or result.url)[0])
                        count = self._no_content_counts.get(counter_key, 0) + 1
                        guidance = []
                        if count >= 2:
                            guidance.append(FIFOGuidance(source='tool', message=build_system_guidence_message(
                                kind='action_guidance',
                                reason=f'同一页面已连续 {count} 次未取得可用正文。读取提示不是网页原文，也不是目标事实的证据。',
                                required_action='根据读取提示决定下一步：若只是没有与当前关注点相关的内容，可在仍服务用户目标的前提下调整关注点，或查看其他页面；'
                                    '若页面需要登录、验证或授权，优先换其他公开来源，不假定改变关注点就能解除限制。'
                                    '同一页面更换关注点后仍无有效信息时，不要反复读取，应转向其他来源；资料仍不足时向用户说明缺口并正常收尾。'
                                    '例如遇到验证码页面时换来源，而不是重复同一读取。')))
                        notice = FIFOExecutionResult(status='succeeded', tool_result={
                            'status': 'no_content', 'has_content': False, 'source_id': source_id,
                            'reason': result.hint, 'no_content_count': count,
                            'message': render_web_page_notice(source, result, count)}, system_guidence=guidance)
                        def commit_notice():
                            # 未提交或任务结束后的迟到结果不能改变后续判断。
                            if not self.closed and (scope is None or scope in self._active_scopes):
                                self._no_content_counts[counter_key] = count
                        if defer_commit:
                            return ToolExecution(notice, after_commit=commit_notice)
                        commit_notice()
                        return notice
                    # 只分页最终文本，片段顺序不变，拼接可恢复完整结果。
                    content = result.content
                    pages = tuple(content[i:i+self.page_chars] for i in range(0, len(content), self.page_chars))
                    snapshot = _Snapshot('web-page:' + str(uuid4()), source_id, source.title, focus, result, pages)
                    if result.images and self._scope.get() is None:
                        return _failure('task_scope_required', '网页图片需要有效任务作用域，请由宿主重新发起读取。')
                    if result.images:
                        snapshot.task_scope = self._scope.get()
                        key = (source_id, focus, snapshot.task_scope)
                    self._items[key] = snapshot
                    while len(self._items) > self.capacity:
                        self._items.popitem(last=False)
                target = snapshot.page + 1 if page is None else page
                if target > len(snapshot.pages):
                    return _failure('end_of_results' if page is None else 'invalid_page',
                        f'已到最后一页，当前第{snapshot.page}/{len(snapshot.pages)}页。' if page is None else
                        f'页码超出范围，可用页码为1至{len(snapshot.pages)}。')
                def commit():
                    if (not self.closed and self._items.get(key) is snapshot
                            and (scope is None or scope in self._active_scopes)):
                        snapshot.page = target
                        self._items.move_to_end(key)
                        self._no_content_counts.pop((scope, urldefrag(snapshot.result.final_url or snapshot.result.url)[0]), None)
                locator, nonce = f'page:{target}', str(uuid4())
                reference = ToolPageReference(resource_id=snapshot.resource_id, locator=locator,
                    display_id=nonce + '.' + self._sign(snapshot.resource_id, locator, nonce),
                    media_type='text', description=f'网页内容，第{target}/{len(snapshot.pages)}页，来源{source_id}')
                references = [reference]
                # 图片集中在第一页，翻页不重复消耗视觉输入；重看第一页会生成新的展示身份。
                if target == 1:
                    for image in snapshot.result.images:
                        location, display = f'image:{image.number}', str(uuid4())
                        references.append(ToolPageReference(resource_id=snapshot.resource_id, locator=location,
                            display_id=display + '.' + self._sign(snapshot.resource_id, location, display),
                            media_type='image', description=f'网页图片：{snapshot.title}\n来源：{snapshot.result.final_url or snapshot.result.url}\n'
                            f'图片编号：{image.number}\n图注：{image.caption or "未提供"}'))
                result = FIFOExecutionResult(status='succeeded', content=FoldableToolContent(pages=references),
                    tool_result={'source_id': source_id, 'focus': snapshot.focus, 'page': target,
                                 'total_pages': len(snapshot.pages), 'image_count': len(references)-1,
                                 'message': '已读取网页内容。', 'image_notice': snapshot.result.hint})
                if defer_commit:
                    return ToolExecution(result, after_commit=commit)
                commit()
                return result
            except PermissionError:
                return _failure('session_expired', '会话已释放，请重新发起读取。')
            except Exception as error:
                logger.warning('网页读取工具执行失败：error_type=%s', type(error).__name__)
                return _failure('web_page_failed', '网页读取暂时失败，请稍后重试，不能据此判断没有相关信息。')

    async def resolve_page(self, reference):
        await self._check_access()
        try:
            nonce, signature = reference.display_id.split('.', 1)
            if reference.media_type not in ('text', 'image') or not hmac.compare_digest(signature,
                    self._sign(reference.resource_id, reference.locator, nonce)):
                raise ValueError()
            prefix, number = reference.locator.split(':', 1)
            page = int(number)
            if prefix != ('image' if reference.media_type == 'image' else 'page') or page < 1:
                raise ValueError()
        except (ValueError, AttributeError):
            raise ValueError('网页页面引用无效，请重新打开网页。') from None
        snapshot = next((item for item in self._items.values() if item.resource_id == reference.resource_id), None)
        if snapshot is not None and snapshot.result.images and snapshot.task_scope != self._scope.get():
            raise ValueError('网页图像不属于当前任务')
        if reference.media_type == 'image':
            if snapshot is None:
                raise ToolPageUnavailable('网页图片已释放，需要时请重新打开来源')
            image = next((image for image in snapshot.result.images if image.number == page), None)
            if image is None:
                raise ValueError('网页图片编号无效')
            return [image.content_block()]
        if snapshot is None:
            text = '先前读取的网页正文已不可用，请使用来源标识和关注重点重新打开。'
        elif page > len(snapshot.pages):
            raise ValueError('网页页码无效，请重新打开网页。')
        else:
            text = render_web_page(snapshot, page)
        return [{'type': 'text', 'text': text}]


def build_open_web_page_registration(viewer):
    async def execute(operation, args):
        return await viewer.open(**args.model_dump(exclude={'keywords'}), defer_commit=True)
    def progress(args):
        focus = args.focus
        if focus is None:
            key = next((key for key in reversed(viewer._items) if key[0] == args.source_id
                        and (len(key) == 2 or key[2] == viewer._scope.get())), None)
            focus = key[1] if key else ''
        terms = args.keywords or re.split(r'[|｜、，,；;\n]+', focus or '')
        # 状态仅展示简短纯文本；不改变原始focus，也不用于缓存键。
        terms = list(dict.fromkeys(' '.join(term.split())[:24] for term in terms if term.strip()))[:5]
        suffix = '：' + ' | '.join(terms) if terms else ''
        return ToolProgress(type='online-search', message='正在读取网页' + suffix)

    return RegisteredTool('open_web_page' , OpenWebPageArguments.__doc__, OpenWebPageArguments, execute,
        return_types=('ordinary', 'foldable'), progress=ToolProgress(type='online-search', message='正在读取网页'), progress_factory=progress)
