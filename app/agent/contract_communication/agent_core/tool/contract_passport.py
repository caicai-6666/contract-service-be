"""通行证合同列表：会话独立 LRU、单工具查询翻页与可折叠渲染。"""
import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import hmac
import logging
import secrets
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.infrastructure.contract_metadata_store import ContractMetadataStatus
from app.schema.agent_tool_content import FoldableToolContent, ToolPageReference
from ..subgraph.fifo_management.schema import FIFOExecutionResult
from .contract_metadata import render_contract_metadata
from .registry import RegisteredTool

logger = logging.getLogger(__name__)


class GetContractByPassportArguments(BaseModel):
    """当你已获得通行证，需要查看它关联的合同列表时，使用这个工具。一张通行证可能关联多份合同；按完整值精确匹配，返回名称、完整合同 ID、摘要、上传人及日期，按入库时间从新到旧展示。首次省略页码读取第1页，后续省略读取下一页；显式传1刷新列表，指定其他页可跳页。结果驻留当前会话，被驱逐后会重新查询。通行证与合同 ID 不同，不能互相替代。可用返回的合同 ID 继续查看原文、注意事项和关联。"""
    model_config = ConfigDict(extra='forbid', frozen=True)
    passport: str = Field(min_length=1, description='用户或可信工具结果提供的完整通行证；保留大小写，不得缩写、猜测补全或用合同 ID、名称、送审消息 ID 替代。不能留空或带首尾空白。翻页仍传同一个通行证。')
    page: int | None = Field(default=None, strict=True, description='从1开始的列表页码；省略或null时首次读取第1页，后续读取下一页。显式传1刷新列表，指定其他页跳页。末页不循环，非法页不改变游标；缓存被驱逐后重新查询，省略页码从第1页开始。这里是合同列表页，不是PDF页码。')

    @field_validator('passport')
    @classmethod
    def validate_passport(cls, value):
        if value != value.strip():
            raise ValueError('请原样提供完整通行证，不要带首尾空白')
        return value


@dataclass
class _Snapshot:
    resource_id: str
    document_ids: tuple[str, ...]
    page: int = 0


def render_contract_passport(*, passport, page, total_pages, total, rows):
    lines = ['# 通行证关联合同', '', f'通行证：{passport}',
             f'第 {page} / {total_pages} 页 · 共 {total} 份合同', '按入库时间从新到旧排列']
    for index, document_id, metadata in rows:
        lines += ['', '---', '', f'## {index}. 合同']
        if metadata is None or metadata.status is not ContractMetadataStatus.READY or metadata.passport != passport:
            lines += [f'合同 ID：{document_id}', '该合同已不可用或不再属于本通行证，请指定第1页刷新列表。']
        else:
            lines += [render_contract_metadata(metadata).replace('# 合同基本信息\n', '', 1).replace('## 合同摘要', '### 合同摘要')]
    lines += ['', f'可继续查看第 {page + 1} 页。' if page < total_pages else '已到最后一页。']
    return '\n'.join(lines)


class ContractPassportViewer:
    """只缓存合同 ID 快照与游标；合同信息在展示时实时读取，不保存页面正文。"""
    def __init__(self, metadata_store, *, max_resident=10, page_size=5):
        if type(max_resident) is not int or max_resident < 1 or type(page_size) is not int or page_size < 1:
            raise ValueError('缓存容量和每页合同数必须为正整数')
        self._metadata = metadata_store
        self._max_resident = max_resident
        self.page_size = page_size
        self._snapshots = OrderedDict()
        self._lock = asyncio.Lock()
        self._key = secrets.token_bytes(32)
        self._closed = False

    def close(self):
        self._closed = True
        self._snapshots.clear()
        self._key = secrets.token_bytes(32)

    def _sign(self, resource, locator, nonce):
        return hmac.new(self._key, f'{resource}\n{locator}\n{nonce}'.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _failure(code, message):
        return FIFOExecutionResult(status='failed', tool_result={'status': 'error', 'code': code, 'message': message})

    async def view(self, passport, page=None):
        args = GetContractByPassportArguments(passport=passport, page=page)
        async with self._lock:
            if self._closed:
                return self._failure('session_unavailable', '会话已释放，请重新发起查询。')
            if args.page is not None and args.page < 1:
                return self._failure('invalid_page', '列表页码从1开始，请指定合法页码。')
            snapshot = self._snapshots.get(passport)
            if snapshot is None or page == 1:
                try:
                    ids = await asyncio.to_thread(self._metadata.list_ids_by_passport, passport)
                except Exception:
                    logger.exception('通行证合同查询失败')
                    return self._failure('query_failed', '查询失败，请稍后重试；不能据此判断没有合同。')
                # 驱逐可在等待线程期间发生，迟到结果不得恢复已关闭会话。
                if self._closed:
                    return self._failure('session_unavailable', '会话已释放，请重新发起查询。')
                if not ids:
                    self._snapshots.pop(passport, None)
                    return FIFOExecutionResult(status='succeeded', tool_result={'status': 'success', 'passport': passport,
                        'count': 0, 'total_pages': 0, 'message': '未找到该通行证关联的已入库合同，请核对通行证。'})
                snapshot = _Snapshot('contract-passport:' + str(uuid4()), tuple(ids))
            pages = (len(snapshot.document_ids) + self.page_size - 1) // self.page_size
            target = page if page is not None else snapshot.page + 1
            if target > pages:
                return self._failure('end_of_results' if page is None else 'invalid_page',
                    f'已到最后一页（共{pages}页）。' if page is None else f'页码超出范围，可用页码为1至{pages}。')
            snapshot.page = target
            self._snapshots[passport] = snapshot
            self._snapshots.move_to_end(passport)
            while len(self._snapshots) > self._max_resident:
                self._snapshots.popitem(last=False)
            locator, nonce = str(target), uuid4().hex
            reference = ToolPageReference(resource_id=snapshot.resource_id, locator=locator,
                display_id=nonce + '.' + self._sign(snapshot.resource_id, locator, nonce), media_type='text',
                description=f'通行证 {passport} 的合同列表，第{target}/{pages}页，共{len(snapshot.document_ids)}份')
            return FIFOExecutionResult(status='succeeded', content=FoldableToolContent(pages=[reference]),
                tool_result={'status': 'success', 'passport': passport, 'page': target,
                             'total_pages': pages, 'count': len(snapshot.document_ids)})

    async def resolve_page(self, reference):
        async with self._lock:
            if self._closed:
                return [{'type': 'text', 'text': '会话已释放，请重新查询通行证。'}]
            try:
                nonce, signature = reference.display_id.split('.', 1)
                page = int(reference.locator)
                if reference.media_type != 'text' or not hmac.compare_digest(signature, self._sign(reference.resource_id, reference.locator, nonce)):
                    raise ValueError('signature')
            except (ValueError, AttributeError) as exc:
                raise ValueError('通行证合同页面引用无效，请重新调用工具。') from exc
            match = next(((p, s) for p, s in self._snapshots.items() if s.resource_id == reference.resource_id), None)
            if match is None:
                return [{'type': 'text', 'text': '通行证合同列表已刷新或被驱逐，请重新调用工具。'}]
            passport, snapshot = match
            pages = (len(snapshot.document_ids) + self.page_size - 1) // self.page_size
            if not 1 <= page <= pages:
                raise ValueError('通行证合同列表页码无效')
            ids = snapshot.document_ids[(page - 1) * self.page_size:page * self.page_size]
            try:
                rows = await asyncio.to_thread(lambda: [(i, doc, self._metadata.get(doc)) for i, doc in enumerate(ids, (page - 1) * self.page_size + 1)])
            except Exception:
                logger.exception('通行证合同页面读取失败')
                return [{'type': 'text', 'text': '合同列表读取失败，请重试，不能据此判断合同不存在。'}]
            if self._closed:
                return [{'type': 'text', 'text': '会话已释放，请重新查询通行证。'}]
            return [{'type': 'text', 'text': render_contract_passport(passport=passport, page=page,
                total_pages=pages, total=len(snapshot.document_ids), rows=rows)}]


def build_contract_passport_registration(viewer):
    async def execute(operation, args):
        return await viewer.view(**args.model_dump())
    return RegisteredTool('get_contract_by_passport', GetContractByPassportArguments.__doc__ + f'每页{viewer.page_size}份合同。',
        GetContractByPassportArguments, execute, return_types=('ordinary', 'foldable'))
