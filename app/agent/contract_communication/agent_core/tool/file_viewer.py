"""文件查看工具：文件模型、缓存池、读取函数与会话查看器。"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Condition, RLock
from dataclasses import dataclass
from typing import TYPE_CHECKING
from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid4
import hashlib
import os
import re
import stat

import asyncio
import base64

import pymupdf
from pydantic import BaseModel, ConfigDict, Field

from app.tool.pdf_open import PDFOpenError, inspect_pdf_openable
from app.tool.pdf_page import (
    CompressedPDFPage, PDFPageRenderConfig, compress_open_pdf_page,
    serialized_pdf_operation,
)


if TYPE_CHECKING:
    from ..subgraph.fifo_management.schema import FIFOExecutionResult
    from app.schema.agent_tool_content import ToolPageReference


FILE_VIEW_TOOLS_VERSION = 'file-view-tools-v3'

class ViewSessionFileArguments(BaseModel):
    """当你需要阅读用户上传附件的原文、核对摘要或确认某页的具体内容时，使用这个工具。一次返回附件的一页图像，帮助依据实际页面作答。先在任务的“用户输入 → 附件”中找到目标文件，将其“内部索引”填入 file_id。可以指定页码，也可以不填页码继续看该文件的下一页。返回结果会说明当前页和总页数；查看失败时会说明原因，阅读位置保持不变。"""

    model_config = ConfigDict(extra='forbid', strict=True)

    file_id: str = Field(
        min_length=36, max_length=36,
        pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
        description='目标附件下“内部索引”一栏的完整值，直接原样复制；不要填写“附件1”等序号、“文件名”或“展示名称”。',
    )
    page_number: int | None = Field(
        default=None, gt=0,
        description='要查看的页码，填从1开始的整数，按文件页面顺序计数，不按正文印刷页码。省略或填 null 时，查看本会话上次成功查看该文件的下一页；从未查看则打开第1页。例如上次看了第3页，不填就打开第4页；重看第3页需填写3。超过总页数会报错。',
    )


class ViewContractFileArguments(BaseModel):
    """当你需要阅读合同库中的合同原文、核实条款或确认摘要未包含的细节时，使用这个工具。一次返回合同的一页图像，帮助依据实际条款作答。将上下文中已提供的目标合同文件标识填入 file_id；只有合同名称或合同编号时不能据此打开。可以指定页码，也可以不填页码继续看该文件的下一页。返回结果会说明当前页和总页数；查看失败时会说明原因，阅读位置保持不变。"""

    model_config = ConfigDict(extra='forbid', strict=True)

    file_id: str = Field(
        min_length=64, max_length=64, pattern=r'^[0-9a-f]{64}$',
        description='上下文中已提供的目标合同文件标识（file_id），原样复制完整值；不是合同名称、合同编号，也不是用户附件下的“内部索引”。未获得文件标识时不要猜测。',
    )
    page_number: int | None = Field(
        default=None, gt=0,
        description='要查看的页码，填从1开始的整数，按文件页面顺序计数，不按正文印刷页码。省略或填 null 时，查看本会话上次成功查看该文件的下一页；从未查看则打开第1页。例如上次看了第3页，不填就打开第4页；重看第3页需填写3。超过总页数会报错。',
    )


def build_file_view_tools() -> list[dict]:
    """从两个参数模型生成工具定义；正式注册处理器后复用同一描述和模型。"""
    return [
        {'type': 'function', 'function': {
            'name': name, 'description': model.__doc__.strip(),
            'parameters': model.model_json_schema(), 'strict': False,
        }}
        for name, model in (
            ('view_session_file', ViewSessionFileArguments),
            ('view_contract_file', ViewContractFileArguments),
        )
    ]


@dataclass(frozen=True)
class FileModelKey:
    """程序生成的缓存身份，不作为模型工具参数。

    namespace 区分附件与合同等来源；scope 标识用户/会话隔离范围或
    公共合同范围，不能包含用户密钥；revision 为来源确认的版本或指纹。
    """
    namespace: str
    scope: str
    file_id: str
    revision: str


@dataclass(frozen=True)
class FileDescriptor:
    """来源已确认的文件身份与名称，不持有原始 PDF 或页面副本。"""
    key: FileModelKey
    file_name: str


# 函数由宿主用 partial 或闭包绑定目录、用户及会话，无需读取器实例。
FileReader = Callable[[str], bytes]
FileResolver = Callable[[str], FileDescriptor]


def read_contract_file(file_id: str, *, directory: Path | None = None) -> bytes:
    """读取 contract 下的正式 PDF；file_id 为小写 SHA-256，不接受路径或 URI。

    仅取得字节，不打开 PDF 或创建模型；目录由可信程序配置，不来自工具参数。
    """
    if not isinstance(file_id, str) or re.fullmatch(r'[0-9a-f]{64}', file_id) is None:
        raise ValueError('合同文件标识必须为64位小写 SHA-256')
    if directory is None:
        from app.infrastructure.pdf_candidate_loader import DEFAULT_CONTRACT_FILE_ROOT
        directory = DEFAULT_CONTRACT_FILE_ROOT
    descriptor = os.open(Path(directory) / f'{file_id}.pdf',
                         os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError('合同文件不是普通文件')
        content = handle.read()
    if not content or hashlib.sha256(content).hexdigest() != file_id:
        raise ValueError('合同文件内容与标识不一致')
    return content


def read_session_file(
    file_id: str, *, authorize: Callable[[str], bool], directory: Path | None = None,
) -> bytes:
    """只读 upload 下已落盘的附件；authorize 必须绑定当前用户和会话。

    授权函数由宿主提供，检查当前附件归属、驻留与准入状态，明确返回 True
    才允许读取；目录存在或 UUID 正确不能替代授权。不自动读取内存或落盘。
    """
    from app.service.communication_files import read_uploaded_pdf

    if not isinstance(file_id, str) or str(UUID(file_id)) != file_id:
        raise ValueError('附件标识必须为规范 UUID')
    if authorize(file_id) is not True:
        raise PermissionError('会话附件不存在或不可用')
    if directory is None:
        from app.core.config import get_settings
        directory = get_settings().communication_database_path.parent / 'upload'
    content = read_uploaded_pdf(Path(directory), file_id)
    # 读取期间发生撤销时不返回文件内容；检查函数不得只凭文件存在放行。
    if authorize(file_id) is not True:
        raise PermissionError('会话附件不存在或不可用')
    return content


class FileModel:
    """来源层提供受权 PDF 字节；本类不读取路径、不管理身份或翻页游标。

    打开、取页和关闭共用项目级 PDF 锁，避免共享模型的读取与释放竞争。
    仅缓存不可变 PNG 页面对象，模型消息中的 Base64 按需构造，不常驻。
    """

    @serialized_pdf_operation
    def __init__(
        self, content: bytes, *, config: PDFPageRenderConfig | None = None,
    ) -> None:
        if config is not None and not isinstance(config, PDFPageRenderConfig):
            raise TypeError('config 必须为 PDFPageRenderConfig')
        # 复用现有真实格式、密码及空页检查；失败时检查函数会自行关闭文档。
        page_count = inspect_pdf_openable(content)
        self._config = config if config is not None else PDFPageRenderConfig()
        self._pages: dict[int, CompressedPDFPage] = {}
        self._cache_owner: FileModelCache | None = None
        self._page_count = page_count
        self._document: pymupdf.Document | None = pymupdf.open(stream=content)

    @classmethod
    def open(cls, content: bytes, *, config: PDFPageRenderConfig | None = None) -> FileModel:
        """打开内存 PDF，尚不渲染页面；调用方负责最终 close 或 aclose。"""
        return cls(content, config=config)

    @property
    def page_count(self) -> int:
        """实际总页数；关闭后仍可读取这项轻量元数据。"""
        return self._page_count

    @property
    @serialized_pdf_operation
    def closed(self) -> bool:
        return self._document is None

    @property
    @serialized_pdf_operation
    def cached_page_numbers(self) -> tuple[int, ...]:
        """已渲染页码的只读快照，不暴露内部缓存容器。"""
        return tuple(sorted(self._pages))

    @serialized_pdf_operation
    def get_page(self, page_number: int) -> CompressedPDFPage:
        """按1起始页码获取页面；首次渲染，重复访问返回同一不可变对象。"""
        if self._document is None:
            raise RuntimeError('文件模型已关闭，请重新打开文件')
        if type(page_number) is not int:
            raise TypeError('页码必须为整数')
        if not 1 <= page_number <= self._page_count:
            raise IndexError(f'页码必须在 1 到 {self._page_count} 之间')
        if page_number not in self._pages:
            # 渲染完全成功后才提交，失败不会污染缓存；整段在同一 PDF 锁内。
            page = compress_open_pdf_page(
                self._document[page_number - 1], page_number, self._config,
            )
            self._pages[page_number] = page
        return self._pages[page_number]

    async def render_page(self, page_number: int) -> list[dict]:
        """适配现有异步页面解析契约；返回独立消息，不在缓存保存 Base64。"""
        def render():
            page = self.get_page(page_number)
            encoded = base64.b64encode(page.png_bytes).decode('ascii')
            return [{'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{encoded}'}}]
        # 取消等待不会中止已开始的底层渲染；close 会通过共享锁等待它结束。
        return await asyncio.to_thread(render)

    @serialized_pdf_operation
    def clear_page_cache(self) -> None:
        """释放本模型持有的页面缓存，保留打开的文件供后续重新渲染。"""
        self._pages.clear()

    @serialized_pdf_operation
    def close(self) -> None:
        """幂等释放文件句柄和缓存；外部仍持有的页面对象不受影响。"""
        if self._document is not None:
            self._document.close()
            self._document = None
        self._pages.clear()

    async def aclose(self) -> None:
        """在线程中释放资源，避免等待在途 PDF 操作时阻塞事件循环。"""
        await asyncio.to_thread(self.close)

    def __enter__(self) -> FileModel:
        if self.closed:
            raise RuntimeError('文件模型已关闭，请重新打开文件')
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


@dataclass(frozen=True)
class CachedFileInfo:
    """文件池目录快照，不暴露可变模型或页面内容。"""
    file: FileDescriptor
    page_count: int
    cached_page_count: int
    added_at: datetime
    last_accessed_at: datetime
    active_readers: int


@dataclass
class _CachedFile:
    file: FileDescriptor
    model: FileModel
    added_at: datetime
    last_accessed_at: datetime
    readers: int = 0


class FileCacheFullError(RuntimeError):
    """容量已满且所有候选正在使用，入池未发生。"""


class FileCacheBusyError(RuntimeError):
    """指定模型正在使用，不能替换或驱逐。"""


# 跨池共用所有权锁，防止同一模型被两个池同时接管；PDF 操作仍使用原有锁。
_FILE_CACHE_LOCK = RLock()


class FileModelCache:
    """具体文件池，按最大文件数执行 LRU；翻页位置仍由查看器管理。

    同步方法可从工作线程调用。acquire 的模型仅在 with 范围内有效，
    不得自行关闭或在离开范围后继续使用。put 成功后生命周期移交给池。
    """

    def __init__(self, *, max_files: int) -> None:
        if type(max_files) is not int or max_files < 1:
            raise ValueError('max_files 必须为正整数')
        self._max_files = max_files
        self._entries: OrderedDict[FileModelKey, _CachedFile] = OrderedDict()
        self._condition = Condition(_FILE_CACHE_LOCK)
        self._closing = False
        self._closed = False

    def _require_open(self) -> None:
        if self._closing:
            raise RuntimeError('文件池已关闭或正在关闭')

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def _remove(self, key: FileModelKey) -> None:
        # 仅在锁内、无借用者时调用；先成功释放再移除，失败保留可追踪条目。
        entry = self._entries[key]
        entry.model.close()
        del self._entries[key]
        entry.model._cache_owner = None

    def put(self, file: FileDescriptor, model: FileModel) -> None:
        """保存模型，必要时替换或驱逐闲置 LRU；失败不接管传入模型。"""
        if not isinstance(file, FileDescriptor) or not isinstance(file.key, FileModelKey):
            raise TypeError('file 必须为有效 FileDescriptor')
        if not isinstance(model, FileModel):
            raise TypeError('model 必须为 FileModel')
        with self._condition:
            self._require_open()
            if model.closed:
                raise ValueError('不能加入已关闭的文件模型')
            old = self._entries.get(file.key)
            if old is not None and old.model is model:
                if old.file != file:
                    raise ValueError('同一模型的文件描述不能改变')
                return
            if model._cache_owner is not None:
                raise ValueError('模型已归属文件池，不能重复接管')
            if old is not None:
                if old.readers:
                    raise FileCacheBusyError('文件正在使用，不能替换')
                self._remove(file.key)
            elif len(self._entries) >= self._max_files:
                victim = next((key for key, entry in self._entries.items() if not entry.readers), None)
                if victim is None:
                    raise FileCacheFullError('文件池已满，全部文件正在使用')
                self._remove(victim)
            now = datetime.now(timezone.utc)
            self._entries[file.key] = _CachedFile(file, model, now, now)
            model._cache_owner = self

    @contextmanager
    def acquire(self, key: FileModelKey):
        """借用模型并保护其不被驱逐；未命中抛 KeyError，不隐式打开文件。"""
        with self._condition:
            self._require_open()
            entry = self._entries[key]
            entry.readers += 1
            entry.last_accessed_at = datetime.now(timezone.utc)
            self._entries.move_to_end(key)
        try:
            yield entry.model
        finally:
            with self._condition:
                entry.readers -= 1
                self._condition.notify_all()

    @contextmanager
    def acquire_or_load(self, file: FileDescriptor, load: Callable[[], FileModel]):
        """原子完成缺失重建与借用，避免入池后尚未取页就被另一查看器驱逐。

        初版在池锁内串行首次加载，合并并发同键加载；加载函数不能回调查看器。
        """
        with self._condition:
            self._require_open()
            if file.key not in self._entries:
                model = load()
                try:
                    self.put(file, model)
                except BaseException:
                    model.close()
                    raise
            lease = self.acquire(file.key)
            model = lease.__enter__()
        try:
            yield model
        finally:
            lease.__exit__(None, None, None)

    def get_page(self, key: FileModelKey, page_number: int) -> CompressedPDFPage:
        """保护取页过程；返回不可变页面，离开借用后仍可安全使用该页面。"""
        with self.acquire(key) as model:
            return model.get_page(page_number)

    def list_files(self) -> tuple[CachedFileInfo, ...]:
        """按最久未访问到最近访问列出目录，不更新 LRU；关闭完成后为空。"""
        with self._condition:
            return tuple(CachedFileInfo(
                entry.file, entry.model.page_count, len(entry.model.cached_page_numbers),
                entry.added_at, entry.last_accessed_at, entry.readers,
            ) for entry in self._entries.values())

    def invalidate(self, key: FileModelKey) -> bool:
        """驱逐单文件；不存在返回 False，正在使用则报忙，不强制关闭。"""
        with self._condition:
            self._require_open()
            entry = self._entries.get(key)
            if entry is None:
                return False
            if entry.readers:
                raise FileCacheBusyError('文件正在使用，不能驱逐')
            self._remove(key)
            return True

    def close(self) -> None:
        """停止新操作，等待全部借用归还后整体释放；不得在自己的借用块内调用。

        同步等待可能阻塞，异步宿主使用 aclose。关闭不删除任何原始文件。
        """
        with self._condition:
            if self._closed:
                return
            if self._closing:
                self._condition.wait_for(lambda: self._closed or not self._closing)
                return self.close()
            self._closing = True
            self._condition.wait_for(lambda: all(not e.readers for e in self._entries.values()))
            errors = []
            for key in tuple(self._entries):
                try:
                    self._remove(key)
                except Exception as exc:
                    errors.append(exc)
            self._closed = not errors
            if errors:
                self._closing = False
            self._condition.notify_all()
            if errors:
                raise ExceptionGroup('部分文件资源释放失败', errors)

    async def aclose(self) -> None:
        """取消等待不取消后台释放，后台会继续等待在途使用结束并关闭。"""
        await asyncio.to_thread(self.close)


class FileViewError(RuntimeError):
    """可反馈给模型的文件查看失败，不携带底层路径或异常堆栈。"""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class FileViewer:
    """会话独立游标与展示引用；缓存可共享，来源函数必须检查当前访问范围。"""

    def __init__(
        self, conversation_id: str, *, resolve_file: FileResolver, read_file: FileReader,
        cache: FileModelCache, config: PDFPageRenderConfig | None = None,
    ) -> None:
        if not conversation_id or not callable(resolve_file) or not callable(read_file):
            raise ValueError('必须提供会话身份及文件定位、读取函数')
        self._conversation_id = conversation_id
        self._resolve_file = resolve_file
        self._read_file = read_file
        self._cache = cache
        self._config = config
        self._positions: dict[FileModelKey, int] = {}
        self._displays: dict[str, tuple] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    def _require_open(self):
        if self._closed:
            raise FileViewError('viewer_closed', '文件查看器已关闭，当前无法查看文件。')

    def _resolve(self, file_id: str) -> FileDescriptor:
        file = self._resolve_file(file_id)
        if not isinstance(file, FileDescriptor) or file.key.file_id != file_id:
            raise FileViewError('invalid_source', '文件来源返回的标识不一致，请重新定位文件。')
        if not file.file_name or not all((file.key.namespace, file.key.scope, file.key.revision)):
            raise FileViewError('invalid_source', '文件来源信息不完整，请重新定位文件。')
        return file

    def _load(self, file: FileDescriptor) -> FileModel:
        content = self._read_file(file.key.file_id)
        if self._resolve(file.key.file_id) != file:
            raise FileViewError('file_changed', '文件已发生变化，请重新打开后查看。')
        # 哈希版本直接核对内容；其他版本号的一致性由可信来源函数保证。
        if re.fullmatch(r'[0-9a-f]{64}', file.key.revision):
            if hashlib.sha256(content).hexdigest() != file.key.revision:
                raise FileViewError('file_changed', '文件内容与版本不一致，请重新定位文件。')
        return FileModel.open(content, config=self._config)

    def _access(self, file_id, page_number=None, *, expected=None, next_page=False):
        file = self._resolve(file_id)  # 缓存命中也不能绕过来源与权限检查。
        if expected is not None and file.key != expected:
            raise FileViewError('file_changed', '该页面对应的文件版本已变化，请重新调用查看工具。')
        with self._cache.acquire_or_load(file, lambda: self._load(file)) as model:
            if page_number is not None and not 1 <= page_number <= model.page_count:
                # 只有省略页码的顺序翻页返回末尾提示；显式越界始终视为非法页码。
                if next_page and page_number > model.page_count:
                    raise FileViewError('end_of_file',
                        f'已到文件最后一页（共 {model.page_count} 页），没有下一页；可指定页码重新查看。')
                raise FileViewError('page_out_of_range',
                    f'页码 {page_number} 超出范围，文件共 {model.page_count} 页，请指定 1 至 {model.page_count} 之间的页码。')
            page = model.get_page(page_number) if page_number is not None else None
            if self._resolve(file_id) != file:
                raise FileViewError('file_changed', '文件已发生变化，请重新打开后查看。')
            return file, page, model.page_count

    @staticmethod
    def _error(exc: Exception) -> FileViewError:
        if isinstance(exc, FileViewError):
            return exc
        if isinstance(exc, PermissionError):
            return FileViewError('file_unavailable', '文件不存在或当前会话无权访问，请核对文件引用。')
        if isinstance(exc, (FileNotFoundError, KeyError)):
            return FileViewError('file_not_found', '文件不存在或尚未保存，请确认文件已可用后重试。')
        if isinstance(exc, PDFOpenError):
            return FileViewError('invalid_pdf', '文件无法打开，可能已损坏、为空或需要密码，请提供可读取的 PDF。')
        if isinstance(exc, (FileCacheFullError, FileCacheBusyError)):
            return FileViewError('cache_busy', '文件池暂时没有可用容量，请稍后重试。')
        if isinstance(exc, (TypeError, ValueError)):
            return FileViewError('invalid_file', '文件标识、内容或来源信息无效，请核对后重试。')
        if isinstance(exc, OSError):
            return FileViewError('read_failed', '文件暂时无法读取，请稍后重试。')
        return FileViewError('view_failed', '文件查看暂时失败，请稍后重试。')

    async def snapshot_pages(self, file_id: str):
        """获取全文件不可变页面快照，复用驻留模型且不改变阅读游标。"""
        def snapshot():
            self._require_open()
            file = self._resolve(file_id)
            # 租约覆盖全部渲染，LRU 不能在读取期间关闭模型。
            with self._cache.acquire_or_load(file, lambda: self._load(file)) as model:
                pages = tuple(model.get_page(number) for number in range(1, model.page_count + 1))
                if self._resolve(file_id) != file:
                    raise FileViewError('file_changed', '文件已发生变化，请重新定位文件。')
                return file, pages
        async with self._lock:
            self._require_open()
            try:
                result = await asyncio.to_thread(snapshot)
                self._require_open()
                return result
            except Exception as exc:
                raise self._error(exc) from exc

    async def open(self, file_id: str) -> FileDescriptor:
        """确保缓存中存在模型，不推进游标；失败抛出带稳定代码的 FileViewError。"""
        async with self._lock:
            try:
                self._require_open()
                file, _, _ = await asyncio.to_thread(self._access, file_id)
                return file
            except Exception as exc:
                raise self._error(exc) from exc

    async def view(self, file_id: str, page_number: int | None = None) -> FIFOExecutionResult:
        """成功返回 foldable 引用；错误返回普通工具反馈，失败不更新阅读位置。"""
        from ..subgraph.fifo_management.schema import FIFOExecutionResult
        from app.schema.agent_tool_content import FoldableToolContent, ToolPageReference

        async with self._lock:
            try:
                self._require_open()
                if not isinstance(file_id, str) or not file_id.strip():
                    raise FileViewError('invalid_arguments', 'file_id 必须为非空文件标识。')
                if page_number is not None and (type(page_number) is not int or page_number < 1):
                    raise FileViewError('invalid_arguments', '页码必须为从1开始的整数，省略表示下一页。')
                file = await asyncio.to_thread(self._resolve, file_id)
                target = page_number if page_number is not None else self._positions.get(file.key, 0) + 1
                file, _, count = await asyncio.to_thread(self._access, file_id, target, expected=file.key, next_page=page_number is None)
                display_id = str(uuid4())
                ref = ToolPageReference(
                    resource_id='file:' + hashlib.sha256(repr(file.key).encode()).hexdigest(),
                    display_id=display_id, locator=f'page:{target}', media_type='image',
                    description=f'{file.file_name}，第{target}页，共{count}页',
                )
                result = FIFOExecutionResult(status='succeeded', content=FoldableToolContent(pages=[ref]),
                    tool_result={'status': 'success', 'file_id': file_id, 'file_name': file.file_name,
                                 'page_number': target, 'page_count': count,
                                 'message': '页面已准备好，请阅读随后展示的文件内容。'})
                # 只保存轻量、不可由调用方修改的引用；完整图片由 resolve_page 按需取得。
                self._displays[display_id] = (ref.model_copy(deep=True), file.key, target)
                self._positions[file.key] = target
                return result
            except Exception as exc:
                error = self._error(exc)
                return FIFOExecutionResult(status='failed', tool_result={
                    'status': 'error', 'code': error.code, 'error': str(error),
                })

    async def resolve_page(self, reference: ToolPageReference) -> list[dict]:
        """可直接注入 PageDisplayWindow；重建被驱逐模型，不推进位置或续发展示。"""
        async with self._lock:
            try:
                self._require_open()
                saved = self._displays.get(reference.display_id)
                if saved is None or reference != saved[0]:
                    raise FileViewError('invalid_reference', '页面引用无效，请重新调用文件查看工具。')
                _, key, number = saved
                _, page, _ = await asyncio.to_thread(self._access, key.file_id, number, expected=key)
                encoded = await asyncio.to_thread(lambda: base64.b64encode(page.png_bytes).decode('ascii'))
                return [{'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{encoded}'}}]
            except Exception as exc:
                # 解析器不能用伪造图片掩盖失败；外层展示流程按既有失败边界停止。
                raise self._error(exc) from exc

    async def aclose(self) -> None:
        """清除当前会话查看状态，禁止新请求；不关闭宿主拥有的文件池。"""
        async with self._lock:
            self._closed = True
            self._positions.clear()
            self._displays.clear()


def build_file_view_registrations(*, session_viewer: FileViewer, contract_viewer: FileViewer):
    """注册项与独立工具定义复用相同名称、类描述和参数 Schema。"""
    from .registry import RegisteredTool

    def handler(viewer):
        async def execute(operation, arguments):
            return await viewer.view(**arguments.model_dump())
        return execute

    return [RegisteredTool(name, model.__doc__.strip(), model, handler(viewer),
                           return_types=('foldable',))
            for name, model, viewer in (
                ('view_session_file', ViewSessionFileArguments, session_viewer),
                ('view_contract_file', ViewContractFileArguments, contract_viewer),
            )]
