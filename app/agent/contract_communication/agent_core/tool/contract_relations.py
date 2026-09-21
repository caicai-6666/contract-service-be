"""合同一跳关联工具与渲染器；每个驻留会话独立实例，由正式宿主装配。"""
import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from datetime import timedelta, timezone
import hashlib
import hmac
import logging
import secrets
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.infrastructure.contract_metadata_store import ContractMetadataStatus
from app.infrastructure.contract_graph_store import ContractGraphNodeMissingError
from app.schema.agent_tool_content import FoldableToolContent, ToolPageReference
from app.service.contract_ingestion import ContractDocumentNotFoundError, ContractDocumentConflictError
from ..subgraph.fifo_management.schema import FIFOExecutionResult
from .registry import RegisteredTool

logger = logging.getLogger(__name__)


class ViewContractRelationsArguments(BaseModel):
    """当你需要了解一份合同与其他合同的直接关联，或为当前分析补充关联背景时，使用这个工具。帮助快速理解合同之间的联系，定位值得进一步阅读的相关合同，减少逐份查找。仅返回一跳关系，不递归展开；结果包含对方合同名称、完整ID、用户填写的关系说明及创建人和时间，可依据返回ID继续查看相关合同。关系说明不是合同原文，不能仅凭关联推断法律效力。每页5条，省略页码首次查看第1页、后续查看下一页；显式指定第1页刷新。"""
    model_config = ConfigDict(extra='forbid', frozen=True)
    document_id: str = Field(pattern=r'^[0-9a-f]{64}$', description='要查看关联的起点合同ID，必须从用户引用或工具结果中原样复制完整的64位小写十六进制标识。禁止缩写、截断、使用省略号或仅保留首尾片段；不得用合同名称、文件名、关系ID替代。若上下文仅有缩写ID，应先取得完整ID，不得猜测补全。')
    page: int | None = Field(default=None, strict=True, description='从1开始的页码；省略或null首次查看第1页、后续查看下一页。显式传1刷新关系快照；末页不循环，越界不移动游标。驻留结果被驱逐后重新从第1页开始。')


@dataclass
class _RelationSnapshot:
    resource_id: str
    relation_ids: tuple[str, ...]
    page: int = 0


def render_contract_relations(*, document_id, file_name, page, total_pages, total, rows):
    """仅排版来源事实；rows 中 None 表示快照里的关系已失效。"""
    lines = ['# 合同直接关联', '', f'起点合同：{file_name}', f'合同 ID：{document_id}',
             '关联范围：一跳，仅包含直接关联', f'第 {page} / {total_pages} 页 · 共 {total} 条快照关系',
             '', '以下关系说明由用户填写，不属于合同原文，也不单独证明法律效力。']
    for index, relation, name in rows:
        if relation is None:
            lines += ['', f'## {index}. 关联已失效', '该关系或对方合同已不可用，请指定第1页刷新。']
            continue
        time = relation.created_at.astimezone(timezone(timedelta(hours=8))).isoformat(sep=' ', timespec='seconds')
        lines += ['', f'## {index}. {name}', f'合同 ID：{relation.document_id}',
                  f'关系 ID：{relation.relation_id}', '关系说明：', relation.description,
                  '', f'创建人：{relation.created_by}', f'创建时间：{time}']
    lines += ['', f'可继续查看第 {page + 1} 页。' if page < total_pages else '已到最后一页。']
    return '\n'.join(lines)


class ContractRelationsViewer:
    """LRU 只保存关系ID快照和游标；页面正文及元数据每次解析实时读取。"""

    def __init__(self, *, relation_service, metadata_store, max_resident=10, page_size=5):
        if type(max_resident) is not int or max_resident < 1:
            raise ValueError('max_resident 必须为正整数')
        if type(page_size) is not int or page_size < 1:
            raise ValueError('page_size 必须为正整数')
        self.page_size = page_size
        self._service = relation_service
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
            raise ContractDocumentConflictError('会话已释放，请重新发起查看。')
        relations = await self._service.list_relations(document_id)
        metadata = await asyncio.to_thread(self._metadata.get, document_id)
        if metadata is None:
            raise ContractDocumentNotFoundError('合同不存在或已删除')
        if metadata.status is not ContractMetadataStatus.READY:
            raise ContractDocumentConflictError('合同尚未入库或正在删除')
        if self._closed:
            raise ContractDocumentConflictError('会话已释放，请重新发起查看。')
        return metadata, sorted(relations, key=lambda row: (-row.created_at.timestamp(), row.relation_id))

    async def view(self, document_id, page=None):
        args = ViewContractRelationsArguments(document_id=document_id, page=page)
        async with self._lock:
            try:
                return await self._view(args.document_id, args.page)
            except (ContractDocumentNotFoundError, ContractDocumentConflictError, ContractGraphNodeMissingError) as exc:
                self._snapshots.pop(document_id, None)
                return self._failure('contract_unavailable', str(exc))
            except Exception:
                logger.exception('合同关联查询失败')
                return self._failure('query_failed', '合同关联查询失败，请稍后重试；不能据此判断没有关联。')

    async def _view(self, document_id, page):
        if page is not None and page < 1:
            return self._failure('invalid_page', '页码从1开始，请指定合法页码。')
        _, relations = await self._read(document_id)
        snapshot = self._snapshots.get(document_id)
        reloaded = snapshot is None
        if snapshot is None or page == 1:
            snapshot = _RelationSnapshot('contract-relations:' + str(uuid4()), tuple(r.relation_id for r in relations))
        total = len(snapshot.relation_ids)
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
                'total': 0, 'total_pages': 0, 'message': '该合同暂无直接关联；指定第1页可刷新。'})
        locator, nonce = f'page:{target}', str(uuid4())
        ref = ToolPageReference(resource_id=snapshot.resource_id, locator=locator,
            display_id=nonce + '.' + self._sign(snapshot.resource_id, locator, nonce), media_type='text',
            description=f'合同 {document_id} 的直接关联，第{target}/{pages}页，共{total}条')
        return FIFOExecutionResult(status='succeeded', content=FoldableToolContent(pages=[ref]),
            tool_result={'status': 'success', 'document_id': document_id, 'page': target, 'total_pages': pages,
                         'total': total, 'message': '已重新加载关联快照。' if reloaded or page == 1 else '已读取关联页。'})

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
                raise ValueError('合同关联页面引用无效，请重新调用查看工具。') from exc
            match = next(((doc, snap) for doc, snap in self._snapshots.items() if snap.resource_id == reference.resource_id), None)
            if match is None:
                return [{'type': 'text', 'text': '关联快照已释放或刷新，请重新调用工具查看。'}]
            document_id, snapshot = match
            try:
                metadata, relations = await self._read(document_id)
                current = {r.relation_id: r for r in relations}
                ids = snapshot.relation_ids[(page - 1) * self.page_size:page * self.page_size]
                def read_rows():
                    rows = []
                    for index, relation_id in enumerate(ids, (page - 1) * self.page_size + 1):
                        relation = current.get(relation_id)
                        other = self._metadata.get(relation.document_id) if relation else None
                        visible = other is not None and other.status is ContractMetadataStatus.READY
                        rows.append((index, relation if visible else None, other.file_name if visible else None))
                    return rows
                rows = await asyncio.to_thread(read_rows)
                text = render_contract_relations(document_id=document_id, file_name=metadata.file_name,
                    page=page, total_pages=(len(snapshot.relation_ids) + self.page_size - 1) // self.page_size,
                    total=len(snapshot.relation_ids), rows=rows)
            except (ContractDocumentNotFoundError, ContractDocumentConflictError, ContractGraphNodeMissingError):
                text = '合同或图节点已不可用，请重新确认合同后查看。'
            except Exception:
                logger.exception('合同关联页面读取失败')
                text = '合同关联页面读取失败，请重新调用工具重试，不能据此判断没有关联。'
            return [{'type': 'text', 'text': text}]


def build_contract_relations_registration(viewer):
    async def execute(operation, args):
        return await viewer.view(**args.model_dump())
    return RegisteredTool('view_contract_relations', ViewContractRelationsArguments.__doc__.replace('每页5条', f'每页{viewer.page_size}条'),
                          ViewContractRelationsArguments, execute, return_types=('ordinary', 'foldable'))
