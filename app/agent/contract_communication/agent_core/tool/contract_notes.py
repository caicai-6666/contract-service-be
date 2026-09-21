"""合同注意事项工具与渲染器；每个驻留会话独立实例，由正式宿主装配。"""
import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import logging
import secrets
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.infrastructure.contract_metadata_store import (ContractMetadataStatus, ContractMetadataNotFoundError, ContractMetadataStateError)
from app.schema.agent_tool_content import FoldableToolContent, ToolPageReference
from ..subgraph.fifo_management.schema import FIFOExecutionResult
from .registry import RegisteredTool

logger = logging.getLogger(__name__)


class ViewContractNotesArguments(BaseModel):
    """当你需要了解用户为某份合同补充的提醒、风险提示或其他注意事项时，使用这个工具。优先查看最近新增的记录，辅助理解合同与识别需要核实的问题。注意事项由用户填写，不属于合同原文，也不代表内容已经核实。返回内容、撰写人和创建时间。每页5条，省略页码首次查看第1页、后续查看下一页；显式指定第1页刷新。"""
    model_config = ConfigDict(extra='forbid', frozen=True)
    document_id: str = Field(pattern=r'^[0-9a-f]{64}$', description='要查看注意事项的起点合同ID，必须从用户引用或工具结果中原样复制完整的64位小写十六进制标识。禁止缩写、截断、使用省略号或仅保留首尾片段；不得用合同名称、文件名、注意事项ID替代。若上下文仅有缩写ID，应先取得完整ID，不得猜测补全。')
    page: int | None = Field(default=None, strict=True, description='从1开始的页码；省略或null首次查看第1页、后续查看下一页。显式传1刷新注意事项快照；末页不循环，越界不移动游标。驻留结果被驱逐后重新从第1页开始。')


@dataclass
class _NoteSnapshot:
    resource_id: str
    note_ids: tuple[str, ...]
    page: int = 0


def render_contract_notes(*, document_id, file_name, page, total_pages, total, rows):
    """原样展示用户记录，不把提醒加工为已核实事实。"""
    lines = ['# 合同注意事项', '', f'合同名称：{file_name}', f'合同 ID：{document_id}',
             f'第 {page} / {total_pages} 页 · 共 {total} 条快照记录', '按创建时间从新到旧排列',
             '', '以下内容由用户补充，不属于合同原文，需结合依据核实。']
    for index, note in rows:
        if note is None:
            lines += ['', f'## {index}. 注意事项已失效', '该记录已删除，请指定第1页刷新。']
            continue
        time = datetime.fromisoformat(note['created_at']).astimezone(timezone(timedelta(hours=8))).isoformat(sep=' ', timespec='seconds')
        lines += ['', f'## {index}. 注意事项', f"注意事项 ID：{note['note_id']}", '内容：', note['content'],
                  '', f"撰写人：{note['author_name']}", f'创建时间：{time}']
    lines += ['', f'可继续查看第 {page + 1} 页。' if page < total_pages else '已到最后一页。']
    return '\n'.join(lines)


class ContractNotesViewer:
    """LRU 只保存注意事项ID快照和游标；页面正文及元数据每次解析实时读取。"""

    def __init__(self, *, metadata_store, max_resident=10, page_size=5):
        if type(max_resident) is not int or max_resident < 1:
            raise ValueError('max_resident 必须为正整数')
        if type(page_size) is not int or page_size < 1:
            raise ValueError('page_size 必须为正整数')
        self.page_size = page_size
        self._metadata = metadata_store
        self._max_resident = max_resident
        self._snapshots = OrderedDict()
        self._lock = asyncio.Lock()
        self._key = secrets.token_bytes(32)
        self._closed = False

    def close(self):
        """会话驱逐时立即撤销读取资格，迟到查询不能重新建立快照。"""
        self._closed = True
        self.clear()

    def clear(self):
        self._snapshots.clear()
        self._key = secrets.token_bytes(32)

    def _sign(self, resource, locator, nonce):
        return hmac.new(self._key, f'{resource}\n{locator}\n{nonce}'.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _failure(code, message):
        return FIFOExecutionResult(status='failed', tool_result={'status': 'error', 'code': code, 'message': message})

    async def _read(self, document_id):
        if self._closed:
            raise ContractMetadataStateError('会话已释放，请重新发起查看。')
        notes = await asyncio.to_thread(self._metadata.list_notes, document_id)
        metadata = await asyncio.to_thread(self._metadata.get, document_id)
        if metadata is None:
            raise ContractMetadataNotFoundError('合同不存在或已删除')
        if metadata.status is not ContractMetadataStatus.READY:
            raise ContractMetadataStateError('合同尚未入库或正在删除')
        if self._closed:
            raise ContractMetadataStateError('会话已释放，请重新发起查看。')
        return metadata, sorted(notes, key=lambda row: (-datetime.fromisoformat(row['created_at']).timestamp(), row['note_id']))

    async def view(self, document_id, page=None):
        args = ViewContractNotesArguments(document_id=document_id, page=page)
        async with self._lock:
            try:
                return await self._view(args.document_id, args.page)
            except (ContractMetadataNotFoundError, ContractMetadataStateError) as exc:
                self._snapshots.pop(document_id, None)
                return self._failure('contract_unavailable', str(exc))
            except Exception:
                logger.exception('合同注意事项查询失败')
                return self._failure('query_failed', '合同注意事项查询失败，请稍后重试；不能据此判断没有注意事项。')

    async def _view(self, document_id, page):
        if page is not None and page < 1:
            return self._failure('invalid_page', '页码从1开始，请指定合法页码。')
        _, notes = await self._read(document_id)
        snapshot = self._snapshots.get(document_id)
        reloaded = snapshot is None
        if snapshot is None or page == 1:
            snapshot = _NoteSnapshot('contract-notes:' + str(uuid4()), tuple(r['note_id'] for r in notes))
        total = len(snapshot.note_ids)
        pages = (total + self.page_size - 1) // self.page_size
        target = page if page is not None else snapshot.page + 1
        if target > max(1, pages):
            return self._failure('end_of_results' if page is None else 'invalid_page',
                                 '已到最后一页，不再继续翻页。' if page is None else f'页码超出范围，可用页码为1至{max(1, pages)}。')
        snapshot.page = target if total else 0
        self._snapshots[document_id] = snapshot
        self._snapshots.move_to_end(document_id)
        while len(self._snapshots) > self._max_resident:
            self._snapshots.popitem(last=False)
        if not total:
            return FIFOExecutionResult(status='succeeded', tool_result={'status': 'success', 'document_id': document_id,
                'total': 0, 'total_pages': 0, 'message': '该合同暂无注意事项；指定第1页可刷新。'})
        locator, nonce = f'page:{target}', str(uuid4())
        ref = ToolPageReference(resource_id=snapshot.resource_id, locator=locator,
            display_id=nonce + '.' + self._sign(snapshot.resource_id, locator, nonce), media_type='text',
            description=f'合同 {document_id} 的注意事项，第{target}/{pages}页，共{total}条')
        return FIFOExecutionResult(status='succeeded', content=FoldableToolContent(pages=[ref]),
            tool_result={'status': 'success', 'document_id': document_id, 'page': target, 'total_pages': pages,
                         'total': total, 'message': '已重新加载注意事项快照。' if reloaded or page == 1 else '已读取注意事项页。'})

    async def resolve_page(self, reference):
        async with self._lock:
            try:
                nonce, signature = reference.display_id.split('.', 1)
                if reference.media_type != 'text' or not hmac.compare_digest(signature, self._sign(reference.resource_id, reference.locator, nonce)):
                    raise ValueError('invalid reference')
                prefix, value = reference.locator.split(':', 1)
                page = int(value)
                if prefix != 'page' or page < 1:
                    raise ValueError('invalid page')
            except (ValueError, AttributeError) as exc:
                raise ValueError('合同注意事项页面引用无效，请重新调用查看工具。') from exc
            match = next(((doc, snap) for doc, snap in self._snapshots.items() if snap.resource_id == reference.resource_id), None)
            if match is None:
                return [{'type': 'text', 'text': '注意事项快照已释放或刷新，请重新调用工具查看。'}]
            document_id, snapshot = match
            try:
                metadata, notes = await self._read(document_id)
                current = {r['note_id']: r for r in notes}
                ids = snapshot.note_ids[(page - 1) * self.page_size:page * self.page_size]
                rows = [(index, current.get(note_id)) for index, note_id in
                        enumerate(ids, (page - 1) * self.page_size + 1)]
                text = render_contract_notes(document_id=document_id, file_name=metadata.file_name,
                    page=page, total_pages=(len(snapshot.note_ids) + self.page_size - 1) // self.page_size,
                    total=len(snapshot.note_ids), rows=rows)
            except (ContractMetadataNotFoundError, ContractMetadataStateError):
                text = '合同已不可用，请重新确认合同后查看。'
            except Exception:
                logger.exception('合同注意事项页面读取失败')
                text = '合同注意事项页面读取失败，请重新调用工具重试，不能据此判断没有注意事项。'
            return [{'type': 'text', 'text': text}]


def build_contract_notes_registration(viewer):
    async def execute(operation, args):
        return await viewer.view(**args.model_dump())
    return RegisteredTool('view_contract_notes', ViewContractNotesArguments.__doc__.replace('每页5条', f'每页{viewer.page_size}条'),
                          ViewContractNotesArguments, execute, return_types=('ordinary', 'foldable'))
