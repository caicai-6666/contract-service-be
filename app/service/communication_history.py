"""会话唯一有序轨迹：运行时同步更新，终态后台复制，SQLite 补充历史。"""

import asyncio
import logging
import sqlite3
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID, uuid4

from anyio import CancelScope
from pydantic import SecretStr
from starlette.concurrency import run_in_threadpool

from app.infrastructure.communication_store import SQLiteCommunicationStore, CommunicationStoreConflict
from app.schema.communication import ConversationHistoryResponse, ConversationHistoryRecord, TERMINAL_STATUSES, MessageDeltaData, public_event_data
from app.schema.communication_workspace import WorkspacePayload, WorkspaceSnapshot
from app.service.communication_trace import project_event, append_tool
from app.service.communication_files import ensure_uploaded_files, read_uploaded_pdf


logger = logging.getLogger(__name__)


class ConversationNotResidentError(ValueError):
    """会话尚未打开或驻留已清空。"""


class ConversationFileNotFoundError(LookupError):
    """统一隐藏附件不存在、未驻留、未准入和跨用户访问的差异。"""


@dataclass(frozen=True)
class ConversationBackup:
    records: tuple[ConversationHistoryRecord, ...]
    workspace: WorkspaceSnapshot
    expected_workspace_revision: int


@dataclass
class ResidentConversation:
    """同一会话只驻留一份历史与工作区；备份副本不参与后续问答。"""

    history: ConversationHistoryResponse
    workspace: WorkspaceSnapshot
    persisted_workspace_revision: int
    # 失败后保持原快照重试，处理“数据库已提交但调用方未收到成功”的情况。
    pending_backup: ConversationBackup | None = None
    residency_id: str = field(default_factory=lambda: str(uuid4()))
    activity_generation: int = 0
    last_activity: float = 0
    last_idle_scan: float = 0
    idle_scans: int = 0
    uploads: dict[str, bytes] = field(default_factory=dict)


@dataclass(frozen=True)
class ArchiveCandidate:
    conversation_id: str
    residency_id: str
    activity_generation: int
    secret_key: SecretStr = field(repr=False)
    records: tuple[ConversationHistoryRecord, ...]
    force: bool


class ConversationHistoryService:
    def __init__(self, store: SQLiteCommunicationStore, *, clock=time.monotonic) -> None:
        self._store = store
        self._clock = clock
        self._resident: dict[str, ResidentConversation] = {}
        # 初期采用单锁串行提交缓存变更，避免并发刷新重复拼接或删除后重新驻留。
        self._lock = asyncio.Lock()
        self._backup_lock = asyncio.Lock()
        self._persisted: set[str] = set()
        self._owners: dict[str, SecretStr] = {}
        self._dirty = asyncio.Event()
        self._worker: asyncio.Task | None = None
        self.evict_runtime = None

    @property
    def lock(self):
        """事件提交与历史读取共用锁，禁止复制成第二份独立任务轨迹。"""
        return self._lock

    async def open(self, conversation_id: str, *, secret_key: SecretStr) -> ConversationHistoryResponse:
        return await self._load(conversation_id, secret_key=secret_key, refresh=False)

    async def refresh(self, conversation_id: str, *, secret_key: SecretStr) -> ConversationHistoryResponse:
        return await self._load(conversation_id, secret_key=secret_key, refresh=True)

    def _find_file_locked(self, conversation_id: str, file_id: str, secret_key: SecretStr):
        """直接定位指定会话，只查其已驻留任务；不跨会话搜索或查询历史数据库。"""
        resident = self._resident.get(conversation_id)
        if resident is None or self._owners.get(conversation_id) != secret_key:
            raise ConversationFileNotFoundError('会话附件不存在或不可用')
        for record in resident.history.records:
            if record.kind != 'task' or record.status not in {
                'processing', 'completed', 'cancelled', 'superseded', 'failed',
            }:
                continue
            source = record.payload.get('input')
            if not isinstance(source, dict):
                continue
            for file in source.get('files', ()):
                if (file.get('file_id') == file_id and file.get('admission') == 'accepted'
                    and file.get('file_path') == f'/{file_id}.pdf'):
                    return resident, file
        raise ConversationFileNotFoundError('会话附件不存在或不可用')

    async def read_file(self, conversation_id: str, file_id: str, *, secret_key: SecretStr) -> tuple[str, bytes]:
        """准入后即可读内存原 PDF；归档后回退磁盘，不等待任务完成、不触发备份。"""
        try:
            file_id = str(UUID(file_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ConversationFileNotFoundError('会话附件不存在或不可用') from exc
        async with self._lock:
            resident, file = self._find_file_locked(conversation_id, file_id, secret_key)
            name = file['file_name']
            content = resident.uploads.get(file['file_path'])
            if content:
                self._touch_locked(conversation_id)
                return name, content
        # 磁盘 I/O 不占用事件锁，也不将读取副本重新放回常驻缓存。
        try:
            content = await run_in_threadpool(read_uploaded_pdf, self._store.database_path.parent / 'upload', file_id)
        except (OSError, ValueError) as exc:
            raise ConversationFileNotFoundError('会话附件不存在或不可用') from exc
        async with self._lock:
            current, _ = self._find_file_locked(conversation_id, file_id, secret_key)
            if current is not resident:
                raise ConversationFileNotFoundError('会话附件不存在或不可用')
            # 读取期间删除、驱逐或重新打开不能继承旧授权；成功读取算作用户活动。
            self._touch_locked(conversation_id)
        return name, content

    async def _load(self, conversation_id: str, *, secret_key: SecretStr, refresh: bool) -> ConversationHistoryResponse:
        async with self._lock:
            result = await self._load_locked(conversation_id, secret_key=secret_key, refresh=refresh)
            self._touch_locked(conversation_id)
            return result

    def _touch_locked(self, conversation_id):
        resident = self._resident[conversation_id]
        resident.activity_generation += 1
        resident.last_activity = resident.last_idle_scan = self._clock()
        resident.idle_scans = 0

    async def _load_locked(self, conversation_id: str, *, secret_key: SecretStr, refresh: bool) -> ConversationHistoryResponse:
        # 即便命中缓存也重新校验密钥；不把命中缓存视为授权。
        await run_in_threadpool(self._store.read_conversation, conversation_id, secret_key=secret_key)
        resident = self._resident.get(conversation_id)
        current = resident.history if resident else None
        if refresh and current is None:
            raise ConversationNotResidentError("会话尚未打开，请先调用 open")
        start = current.records[0].sequence if current and current.records else None
        page = await run_in_threadpool(
            self._store.read_history_window, conversation_id, secret_key=secret_key,
            start_sequence=start, extend=refresh and start is not None,
            include_workspace=resident is None,
        )
        workspace = WorkspaceSnapshot.model_validate(page.pop('workspace')) if resident is None else resident.workspace
        incoming = tuple(ConversationHistoryRecord.model_validate(row) for row in page.pop("records"))
        records = {r.sequence: r for r in current.records} if current else {}
        for record in incoming:
            previous = records.get(record.sequence)
            if previous is not None and previous != record:
                raise ValueError("已冻结历史发生变化，拒绝覆盖驻留轨迹")
            records[record.sequence] = record
        ordered = tuple(records[seq] for seq in sorted(records))
        latest_summary = next((r.sequence for r in reversed(ordered) if r.kind == "summary"), None)
        result = ConversationHistoryResponse(
            **page, records=ordered,
            model_context_start_sequence=latest_summary or (ordered[0].sequence if ordered else None),
        )
        # 校验完整结果后再替换缓存；返回深拷贝避免外部修改嵌套 payload。
        if resident is None:
            now = self._clock()
            self._resident[conversation_id] = ResidentConversation(
                result, workspace, workspace.revision, last_activity=now, last_idle_scan=now,
            )
        else:
            resident.history = result
        # 在途事务即便已经被读到，也要等“轨迹 + 工作区”整批确认成功后再标记。
        resident_ids = {r.record_id for r in current.records} if current else set()
        self._persisted.update(record.record_id for record in incoming if record.record_id not in resident_ids)
        self._owners[conversation_id] = secret_key
        return result.model_copy(deep=True)

    async def register_locked(self, conversation_id, turn_id, *, secret_key, source, can_interrupt=False):
        """调用者持有共享锁；注册时确定顺序，不能按后台备份完成顺序编号。"""
        await self._load_locked(conversation_id, secret_key=secret_key, refresh=False)
        current = self._resident[conversation_id].history
        if any(r.turn_id == turn_id for r in current.records):
            raise ValueError('轮次已存在于会话历史中')
        files = self._stage_files(source.files if source else ())
        record = ConversationHistoryRecord(
            record_id=str(uuid4()), sequence=max((r.sequence for r in current.records), default=0) + 1,
            kind='task', turn_id=turn_id, status='pending_activation',
            can_interrupt=can_interrupt,
            payload={'input': {'text': source.text if source else None, 'files': files},
                     'trace': [], 'events': [], 'event_cursor': 0},
            created_at=int(datetime.now(timezone.utc).timestamp() * 1000),
        )
        self._resident[conversation_id].history = current.model_copy(update={
            'records': current.records + (record,),
            'model_context_start_sequence': current.model_context_start_sequence or record.sequence,
        })
        self._resident[conversation_id].uploads.update(
            (f"/{saved['file_id']}.pdf", original.content)
            for saved, original in zip(files, source.files if source else (), strict=True)
        )
        self._touch_locked(conversation_id)

    def _stage_files(self, files):
        """只分配身份并暂存字节，不能把注册成功当成门禁通过。"""
        directory = self._store.database_path.parent / 'upload'
        occupied = set()
        for resident in self._resident.values():
            for record in resident.history.records:
                source = record.payload.get('input')
                # 兼容旧轨迹的纯文本 input，不因分配新附件身份而拒绝延续旧会话。
                if isinstance(source, dict):
                    occupied.update(f['file_id'] for f in source.get('files', []) if 'file_id' in f)
        staged = []
        for file in files:
            while True:
                file_id = str(uuid4())
                path = directory / f'{file_id}.pdf'
                if file_id not in occupied and not path.exists() and not path.is_symlink():
                    break
            occupied.add(file_id)
            staged.append({'file_id': file_id, 'file_name': file.file_name,
                           'display_name': None, 'summary': None,
                           'file_path': None, 'admission': 'pending'})
        return staged

    def record_file_summaries_locked(self, conversation_id, turn_id, summaries):
        """在共享锁内整批补充已校验摘要；同名文件依靠上传下标绑定既有 UUID。"""
        record = self._record(conversation_id, turn_id)
        if record.status != 'processing':
            raise ValueError('冻结任务禁止补充文件摘要')
        payload = deepcopy(record.payload)
        files = payload['input']['files']
        if [row.file_index for row in summaries] != list(range(len(files))):
            raise ValueError('摘要下标必须完整对应附件列表')
        for row, file in zip(summaries, files):
            if row.original_file_name != file['file_name']:
                raise ValueError('摘要的原始文件名不匹配')
            if file.get('display_name') is not None or file.get('summary') is not None:
                raise ValueError('文件摘要只允许写入一次')
            # 不复制节点 reasoning、审计或渲染对象，也不赋予附件访问权限。
            file.update(display_name=row.display_name, summary=row.summary)
        self._replace_locked(conversation_id, record.model_copy(update={'payload': payload}))

    def resolve_file_admission_locked(self, conversation_id, turn_id, accepted_indices):
        """只接受执行层明确提交的逐文件结果；终态和迟到结果不可改写。"""
        record = self._record(conversation_id, turn_id)
        if record.status != 'processing':
            raise ValueError('只允许处理中的任务提交附件准入结果')
        payload = deepcopy(record.payload)
        files = payload['input']['files']
        if any(file.get('admission') != 'pending' for file in files):
            raise ValueError('附件准入结果只能提交一次')
        for index, file in enumerate(files):
            accepted = index in accepted_indices
            file.update(admission='accepted' if accepted else 'unavailable',
                        file_path=f"/{file['file_id']}.pdf" if accepted else None)
        self._replace_locked(conversation_id, record.model_copy(update={'payload': payload}))
        self._release_unavailable_locked(conversation_id, files)

    def _release_unavailable_locked(self, conversation_id, files):
        for file in files:
            if file.get('admission') == 'unavailable':
                self._resident[conversation_id].uploads.pop(f"/{file['file_id']}.pdf", None)

    def _record(self, conversation_id, turn_id):
        current = self._resident[conversation_id].history
        return next(r for r in current.records if r.turn_id == turn_id)

    def get_gate_records_locked(self, conversation_id, turn_id) -> tuple[ConversationHistoryRecord, ...]:
        """调用者须持共享锁并校验轮次归属；只复制当前轮之前的最近五条合格任务。

        refresh 可能驻留更早历史，故必须重新定位最新摘要边界；不跨摘要补足数量。
        拒绝、过期及在途任务不参与选择，也不占用五轮额度。累计摘要本身不送模型。
        """
        current = self._record(conversation_id, turn_id)
        previous = [r for r in self._resident[conversation_id].history.records
                    if r.sequence < current.sequence]
        boundary = max((r.sequence for r in previous if r.kind == 'summary'), default=0)
        eligible = sorted((r for r in previous if r.sequence > boundary and r.kind == 'task'
                           and r.status in {'completed', 'superseded', 'cancelled', 'failed'}),
                          key=lambda r: r.sequence)
        # 隔离嵌套 payload，模型渲染不能改写仍在使用的驻留记录。
        return tuple(r.model_copy(deep=True) for r in eligible[-5:])

    def _replace_locked(self, conversation_id, record):
        current = self._resident[conversation_id].history
        self._resident[conversation_id].history = current.model_copy(update={
            'records': tuple(record if r.record_id == record.record_id else r for r in current.records),
        })
        if record.status in TERMINAL_STATUSES:
            self._touch_locked(conversation_id)
            self._dirty.set()

    def accept_events_locked(self, conversation_id, turn_id, entries):
        """一批消息收束与终态共同投影，全部成功后才替换权威历史。"""
        self.commit_projected_events_locked(conversation_id, self.project_events_locked(conversation_id, turn_id, entries))

    def project_events_locked(self, conversation_id, turn_id, entries):
        """无副作用地预校验事件批次，供替代流程在注册新轮次之前使用。"""
        record = self._record(conversation_id, turn_id)
        if record.status in TERMINAL_STATUSES:
            raise ValueError('冻结任务禁止更新')
        payload = record.payload
        for event, snapshot in entries:
            payload = project_event(payload, event.data, snapshot)
            # 精简展示记录不使用有界回放队列，避免缓存淘汰后丢失历史。
            if not isinstance(event.data, MessageDeltaData):
                payload.setdefault('events', []).append({
                    'sequence': event.sequence, 'event': event.data.event_type,
                    'data': public_event_data(event),
                })
            payload['event_cursor'] = event.sequence
        if snapshot.status in TERMINAL_STATUSES:
            for file in payload.get('input', {}).get('files', []):
                # 未取得结果就结束时不推断通过；整轮拒绝优先于先前单文件结果。
                if file.get('admission') == 'pending' or (
                    snapshot.status == 'rejected' and 'admission' in file
                ):
                    file.update(admission='unavailable', file_path=None)
        return record.model_copy(update={
            'payload': payload, 'status': snapshot.status,
            'can_interrupt': snapshot.can_interrupt,
            'processing_duration_ms': snapshot.processing_duration_ms,
            'activated_at': int(snapshot.activated_at.timestamp() * 1000) if snapshot.activated_at else None,
        })

    def commit_projected_events_locked(self, conversation_id, record):
        """调用者须持续持有共享锁，只提交同锁内已经通过校验的投影。"""
        self._replace_locked(conversation_id, record)
        self._release_unavailable_locked(conversation_id, record.payload.get('input', {}).get('files', []))

    def accept_tool_locked(self, conversation_id, turn_id, item):
        record = self._record(conversation_id, turn_id)
        if record.status != 'processing':
            raise ValueError('只允许处理中的任务记录工具调用')
        self._replace_locked(conversation_id, record.model_copy(update={'payload': append_tool(record.payload, item)}))

    async def start(self):
        if self._worker is None:
            self._worker = asyncio.create_task(self._backup_loop())

    async def _backup_loop(self):
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            # 让出当前终态交付周期；备份不在 SSE 提交或用户请求里执行。
            await asyncio.sleep(0.25)
            if not await self.flush():
                await asyncio.sleep(1)
                self._dirty.set()

    async def flush(self, conversation_id: str | None = None) -> bool:
        """按会话原子备份轨迹与工作区；失败保留快照重试，不阻塞运行时更新。"""
        success = True
        async with self._backup_lock:
            async with self._lock:
                candidates = []
                for cid, resident in self._resident.items():
                    if conversation_id is not None and cid != conversation_id:
                        continue
                    if resident.pending_backup is None:
                        records = []
                        for record in resident.history.records:
                            if record.record_id in self._persisted:
                                continue
                            if record.status not in TERMINAL_STATUSES:
                                break
                            records.append(record.model_copy(deep=True))
                        if not records and resident.workspace.revision == resident.persisted_workspace_revision:
                            continue
                        resident.pending_backup = ConversationBackup(
                            tuple(records), resident.workspace.model_copy(deep=True), resident.persisted_workspace_revision,
                        )
                    candidates.append((cid, self._owners[cid], resident.pending_backup, dict(resident.uploads)))
            for cid, key, backup, uploads in candidates:
                try:
                    # 先保存准入通过的文件，再提交引用它们的冻结轨迹；失败保留字节重试。
                    await run_in_threadpool(ensure_uploaded_files,
                                            self._store.database_path.parent / 'upload', backup.records, uploads)
                    await run_in_threadpool(
                        self._store.backup_tasks_with_workspace, cid, secret_key=key,
                        records=[record.model_dump() for record in backup.records],
                        workspace=backup.workspace.model_dump(),
                        expected_workspace_revision=backup.expected_workspace_revision,
                    )
                except (sqlite3.Error, ValueError, LookupError, OSError):
                    # 不回显密钥、正文或数据库异常细节；保留内存供展示和重试。
                    logger.warning('会话轨迹与工作区备份失败，将保留内存并重试：conversation_id=%s', cid)
                    success = False
                else:
                    async with self._lock:
                        resident = self._resident[cid]
                        self._persisted.update(record.record_id for record in backup.records)
                        resident.persisted_workspace_revision = backup.workspace.revision
                        resident.pending_backup = None
                        # 只确认旧快照，不替换加工期间已经更新的内存工作区。
                        if resident.workspace.revision != resident.persisted_workspace_revision or next(
                            (r.status for r in resident.history.records if r.record_id not in self._persisted), None,
                        ) in TERMINAL_STATUSES:
                            self._dirty.set()
        return success

    async def get_workspace(self, conversation_id: str, *, secret_key: SecretStr) -> WorkspaceSnapshot:
        """供后续问答执行器读取工作区；深拷贝禁止调用方绕过校验直接修改驻留状态。"""
        async with self._lock:
            await self._load_locked(conversation_id, secret_key=secret_key, refresh=False)
            return self._resident[conversation_id].workspace.model_copy(deep=True)

    async def update_workspace(
        self, conversation_id: str, *, secret_key: SecretStr, payload: dict, expected_revision: int,
    ) -> WorkspaceSnapshot:
        """接受执行器已确认的完整工作区；只负责校验/存储，不推断或整理业务内容。"""
        validated = WorkspacePayload.model_validate(payload).model_copy(deep=True)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('工作区版本必须为非负整数')
        async with self._lock:
            await self._load_locked(conversation_id, secret_key=secret_key, refresh=False)
            resident = self._resident[conversation_id]
            if resident.workspace.revision != expected_revision:
                raise CommunicationStoreConflict('工作区版本已过期')
            resident.workspace = WorkspaceSnapshot(
                payload=validated, revision=expected_revision + 1,
                updated_at=int(datetime.now(timezone.utc).timestamp() * 1000),
            )
            self._touch_locked(conversation_id)
            self._dirty.set()
            return resident.workspace.model_copy(deep=True)

    async def archive_candidates(self, *, interval: float, idle_limit: int, idle_seconds: float) -> tuple[ArchiveCandidate, ...]:
        """扫描只读取统一驻留数据；单调时钟防止重复扫描或墙钟调整提前触发驱逐。"""
        async with self._lock:
            now = self._clock()
            candidates = []
            for cid, resident in self._resident.items():
                active = any(r.kind == 'task' and r.status not in TERMINAL_STATUSES for r in resident.history.records)
                if active:
                    resident.idle_scans = 0
                    resident.last_idle_scan = now
                elif now - resident.last_idle_scan >= interval:
                    resident.idle_scans += 1
                    resident.last_idle_scan = now
                force = not active and resident.idle_scans >= idle_limit and now - resident.last_activity >= idle_seconds
                candidates.append(ArchiveCandidate(
                    cid, resident.residency_id, resident.activity_generation, self._owners[cid],
                    tuple(r.model_copy(deep=True) for r in resident.history.records), force,
                ))
            return tuple(candidates)

    def _matching_resident(self, candidate):
        resident = self._resident.get(candidate.conversation_id)
        if resident is None or resident.residency_id != candidate.residency_id:
            raise ConversationNotResidentError('归档来源会话已被删除或重新驻留')
        return resident

    async def memory_backlog(self, candidate: ArchiveCandidate) -> tuple[ConversationHistoryRecord, ...]:
        stored = await run_in_threadpool(self._store.read_memory_backlog, candidate.conversation_id,
                                        secret_key=candidate.secret_key)
        async with self._lock:
            self._matching_resident(candidate)
        boundary = max((r.sequence for r in candidate.records), default=0)
        boundary = min((r.sequence - 1 for r in candidate.records
                        if r.kind == 'task' and r.status not in TERMINAL_STATUSES), default=boundary)
        records = {r['record_id']: ConversationHistoryRecord.model_validate(r) for r in stored['records']
                   if r['sequence'] <= boundary}
        for record in candidate.records:
            if record.kind == 'task' and record.sequence <= boundary and record.record_id not in stored['processed_ids']:
                if record.record_id in records and records[record.record_id] != record:
                    raise CommunicationStoreConflict('待加工任务与已备份原轨迹不一致')
                records[record.record_id] = record
        frozen = []
        for record in sorted(records.values(), key=lambda r: r.sequence):
            if record.status not in TERMINAL_STATUSES:
                break
            frozen.append(record)
        return tuple(frozen)

    async def commit_archive(self, candidate: ArchiveCandidate, records, memories) -> None:
        """模型计算不持锁；提交前先解决原始备份回执，之后与备份/删除串行提交。"""
        cid = candidate.conversation_id
        if not await self.flush(cid):
            raise CommunicationStoreConflict('原轨迹备份尚未确认')
        async with self._backup_lock:
            async with self._lock:
                resident = self._matching_resident(candidate)
                if resident.pending_backup is not None:
                    raise CommunicationStoreConflict('原轨迹备份仍待重试')
                workspace = resident.workspace.model_copy(deep=True)
                revision = resident.persisted_workspace_revision
                uploads = dict(resident.uploads)
            await run_in_threadpool(ensure_uploaded_files, self._store.database_path.parent / 'upload', records, uploads)
            await run_in_threadpool(
                self._store.archive_tasks_with_workspace, cid, secret_key=candidate.secret_key,
                records=[r.model_dump() for r in records], memories=[r.model_dump() for r in memories],
                workspace=workspace.model_dump(), expected_workspace_revision=revision,
            )
            async with self._lock:
                resident = self._matching_resident(candidate)
                self._persisted.update(r.record_id for r in records)
                resident.persisted_workspace_revision = workspace.revision
                # 已归档文件仍留在磁盘；只释放本批原始字节，不影响新任务的附件。
                for record in records:
                    for file in record.payload.get('input', {}).get('files', []):
                        resident.uploads.pop(f"/{file['file_id']}.pdf", None)
                if resident.workspace.revision != workspace.revision:
                    self._dirty.set()

    async def evict_archived(self, candidate: ArchiveCandidate) -> bool:
        """驱逐是最终比较并删除操作；新输入、工作区更新或重新打开都使旧资格失效。"""
        if not candidate.force or not await self.flush(candidate.conversation_id):
            return False
        async with self._backup_lock:
            async with self._lock:
                resident = self._matching_resident(candidate)
                records = tuple(r.model_copy(deep=True) for r in resident.history.records)
                uploads = dict(resident.uploads)
            await run_in_threadpool(ensure_uploaded_files, self._store.database_path.parent / 'upload', records, uploads)
            stored = await run_in_threadpool(self._store.read_memory_backlog, candidate.conversation_id,
                                            secret_key=candidate.secret_key)
            async with self._lock:
                resident = self._matching_resident(candidate)
                if (resident.activity_generation != candidate.activity_generation
                    or resident.pending_backup is not None
                    or resident.workspace.revision != resident.persisted_workspace_revision
                    or stored['records']
                    or any(r.record_id not in self._persisted or
                           (r.kind == 'task' and (r.status not in TERMINAL_STATUSES or r.record_id not in stored['processed_ids']))
                           for r in resident.history.records)):
                    return False
                self._persisted.difference_update(r.record_id for r in resident.history.records)
                del self._resident[candidate.conversation_id]
                self._owners.pop(candidate.conversation_id, None)
                if self.evict_runtime:
                    self.evict_runtime(candidate.conversation_id)
                return True

    async def get_model_records(self, conversation_id: str, *, secret_key: SecretStr) -> tuple[ConversationHistoryRecord, ...]:
        """仅返回最新摘要（包含）之后的历史候选；最终提示词与状态过滤由后续 Agent 层负责。"""
        snapshot = await self.open(conversation_id, secret_key=secret_key)
        boundary = snapshot.model_context_start_sequence
        return tuple(r for r in snapshot.records if boundary is not None and r.sequence >= boundary)

    async def delete(self, conversation_id: str, *, secret_key: SecretStr) -> None:
        # 与在途备份串行，保证删除后不被旧备份重新写入；运行时提交共用内层锁。
        async with self._backup_lock, self._lock:
            with CancelScope(shield=True):
                await run_in_threadpool(self._store.delete_conversation, conversation_id, secret_key=secret_key)
                current = self._resident.pop(conversation_id, None)
                if current:
                    self._persisted.difference_update(r.record_id for r in current.history.records)
                self._owners.pop(conversation_id, None)
                if self.evict_runtime:
                    self.evict_runtime(conversation_id)

    async def close(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        # 停止生产后排空旧备份重试及其间产生的新版本；失败不无限阻塞关闭。
        while await self.flush():
            async with self._lock:
                if not any(
                    resident.pending_backup is not None
                    or resident.workspace.revision != resident.persisted_workspace_revision
                    or next((r.status for r in resident.history.records if r.record_id not in self._persisted), None)
                    in TERMINAL_STATUSES
                    for resident in self._resident.values()
                ):
                    break
        async with self._lock:
            self._resident.clear()
            self._persisted.clear()
            self._owners.clear()
