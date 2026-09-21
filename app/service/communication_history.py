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
from app.schema.reasoning_window import ReasoningWindow
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
    positions: dict[str, int] = field(default_factory=dict)


@dataclass
class ResidentConversation:
    """同一会话只驻留一份历史与工作区；备份副本不参与后续问答。"""

    history: ConversationHistoryResponse
    workspace: WorkspaceSnapshot
    persisted_workspace_revision: int
    # 失败后保持原快照重试，处理“数据库已提交但调用方未收到成功”的情况。
    pending_backup: ConversationBackup | None = None
    ephemeral: bool = False
    residency_id: str = field(default_factory=lambda: str(uuid4()))
    activity_generation: int = 0
    last_activity: float = 0
    last_idle_scan: float = 0
    idle_scans: int = 0
    uploads: dict[str, bytes] = field(default_factory=dict)
    reasoning_window: ReasoningWindow = field(default_factory=ReasoningWindow)
    memory_query_pool: object | None = None


@dataclass(frozen=True)
class ArchiveCandidate:
    conversation_id: str
    residency_id: str
    activity_generation: int
    secret_key: SecretStr = field(repr=False)
    records: tuple[ConversationHistoryRecord, ...]
    force: bool


class ConversationHistoryService:
    def __init__(self, store: SQLiteCommunicationStore, *, clock=time.monotonic, memory_query_capacity=10, memory_query_page_size=3) -> None:
        self._memory_query_capacity = memory_query_capacity
        self._memory_query_page_size = memory_query_page_size
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

    async def create_ephemeral(self, conversation_id: str, *, secret_key: SecretStr) -> None:
        """展示任务只建内存工作区；不创建 SQLite 会话，也不进入备份与记忆归档。"""
        from app.schema.communication_workspace import empty_workspace_payload
        async with self._lock:
            if conversation_id in self._resident:
                raise ValueError('会话已存在')
            if sum(r.ephemeral for r in self._resident.values()) >= 32:
                raise ValueError('临时展示会话已满，请稍后重试')
            now = int(datetime.now(timezone.utc).timestamp() * 1000)
            history = ConversationHistoryResponse(conversation_id=conversation_id, name='临时展示',
                created_at=now, records=(), has_more=False, model_context_start_sequence=None)
            workspace = WorkspaceSnapshot(payload=empty_workspace_payload(), revision=0, updated_at=now)
            self._resident[conversation_id] = ResidentConversation(history, workspace, 0,
                ephemeral=True, last_activity=self._clock(), last_idle_scan=self._clock())
            self._owners[conversation_id] = secret_key

    async def owns_ephemeral(self, conversation_id: str, *, secret_key: SecretStr) -> bool:
        async with self._lock:
            resident = self._resident.get(conversation_id)
            return bool(resident and resident.ephemeral and self._owners.get(conversation_id) == secret_key)

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

    async def memory_query_pool(self, conversation_id, *, secret_key):
        """已鉴权驻留会话共用配置容量的查询池；由检索会话注入使用。"""
        from app.agent.contract_communication.agent_core.subgraph.memory_retrieval.pool import MemoryQueryPool
        async with self._lock:
            resident = self._resident.get(conversation_id)
            if resident is None or self._owners.get(conversation_id) != secret_key:
                raise PermissionError('会话不可用')
            if resident.memory_query_pool is None:
                resident.memory_query_pool = MemoryQueryPool(conversation_id, self._store.database_path,
                    capacity=self._memory_query_capacity, page_size=self._memory_query_page_size)
            return resident.memory_query_pool

    async def file_tool_access(self, conversation_id, *, secret_key, file_id=None):
        """文件工具的权限快照；工作线程须桥接到事件循环，不能直接读驻留字典。"""
        async with self._lock:
            resident = self._resident.get(conversation_id)
            if resident is None or self._owners.get(conversation_id) != secret_key:
                raise PermissionError('会话不可用')
            name = None
            if file_id is not None:
                try:
                    _, file = self._find_file_locked(conversation_id, file_id, secret_key)
                except ConversationFileNotFoundError as exc:
                    raise PermissionError('会话附件不可用') from exc
                name = file['file_name']
            return resident.residency_id, name

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
        resident = self._resident.get(conversation_id)
        if resident is not None and resident.ephemeral:
            if self._owners.get(conversation_id) != secret_key:
                raise LookupError('会话不存在')
            return resident.history.model_copy(deep=True)
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
        by_id = {r.record_id: r for r in records.values()}
        for record in incoming:
            # 摘要内存插入后，磁盘可能尚未备份新的排序；身份稳定，采用驻留序号。
            if record.record_id in by_id:
                record = record.model_copy(update={'sequence': by_id[record.record_id].sequence})
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
                reasoning_window=await run_in_threadpool(self._store.read_reasoning_window, conversation_id, secret_key=secret_key),
            )
        else:
            resident.history = result
        # 在途事务即便已经被读到，也要等“轨迹 + 工作区”整批确认成功后再标记。
        resident_ids = {r.record_id for r in current.records} if current else set()
        self._persisted.update(record.record_id for record in incoming if record.record_id not in resident_ids)
        self._owners[conversation_id] = secret_key
        return result.model_copy(deep=True)

    def get_agent_core_snapshot_locked(self, conversation_id, turn_id):
        """调用者持有共享锁并已校验用户与当前轮次；返回同一时点的深拷贝。"""
        resident = self._resident[conversation_id]
        current = self._record(conversation_id, turn_id)
        if current.status != 'processing' or not current.payload.get('agent_core_ready'):
            raise ValueError('当前任务尚未通过门禁或已经结束')
        prior = [r for r in resident.history.records if r.sequence <= current.sequence]
        summary = next((r for r in reversed(prior) if r.kind == 'summary'), None)
        boundary = summary.sequence if summary else 0
        records = tuple(r.model_copy(deep=True) for r in prior
                        if r.kind == 'task' and r.sequence > boundary
                        and (r.turn_id == turn_id or (r.payload.get('agent_core_ready') is True
                             and r.status in {'completed', 'cancelled', 'superseded', 'failed'})))
        return (resident.workspace.model_copy(deep=True),
                deepcopy(summary.payload) if summary else None, records)

    def mark_agent_core_ready_locked(self, conversation_id, turn_id, opened_files):
        """只由正式门禁通过分支调用；元数据写回原任务，不另建上下文存储。"""
        record = self._record(conversation_id, turn_id)
        if record.status != 'processing':
            raise ValueError('已结束任务不能进入 Agent Core')
        payload = deepcopy(record.payload)
        files = payload['input']['files']
        if files or opened_files:
            if len(opened_files) != len(files):
                raise ValueError('门禁页数与附件数量不一致')
            for index, (file, opened) in enumerate(zip(files, opened_files, strict=True)):
                if opened.file_index != index or opened.file_name != file['file_name']:
                    raise ValueError('门禁文件身份不一致')
                file['page_count'] = opened.page_count
        from app.agent.contract_communication.agent_core.context_rendering import TaskUserInput
        TaskUserInput.model_validate({'content': payload['input']['text'],
            'contracts': payload['input'].get('contracts', []), 'files': [
            {key: file.get(key) for key in ('file_id', 'file_name', 'display_name', 'summary', 'page_count')}
            for file in files if file.get('admission') == 'accepted']})
        payload['agent_core_ready'] = True
        self._replace_locked(conversation_id, record.model_copy(update={'payload': payload}))

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
            payload={'input': {'text': source.text if source else None, 'files': files,
                               'contracts': [item.model_dump(mode='json') for item in source.contracts] if source else []},
                     'trace': [], 'events': [], 'event_cursor': 0, 'agent_core_ready': False},
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
            # 操作提示只服务活动任务；在终态投影中清理，验收成功后才原子提交。
            # 私有审计独立保存，不属于此 payload，也不随清理删除。
            if 'agent_messages' in payload:
                from app.agent.contract_communication.agent_core.native_messages import close_native_messages
                payload['agent_messages'] = close_native_messages(payload['agent_messages'])
            if 'trace' in payload:
                payload['trace'] = [item for item in payload['trace'] if item.get('type') != 'system_guidence']
            for file in payload.get('input', {}).get('files', []):
                # 未取得结果就结束时不推断通过；整轮拒绝优先于先前单文件结果。
                if file.get('admission') == 'pending' or (
                    snapshot.status == 'rejected' and 'admission' in file
                ):
                    file.update(admission='unavailable', file_path=None)
        projected = record.model_copy(update={
            'payload': payload, 'status': snapshot.status,
            'can_interrupt': snapshot.can_interrupt,
            'processing_duration_ms': snapshot.processing_duration_ms,
            'activated_at': int(snapshot.activated_at.timestamp() * 1000) if snapshot.activated_at else None,
        })
        if projected.status in {'completed', 'cancelled', 'superseded', 'failed'} and projected.payload.get('agent_core_ready'):
            from app.agent.contract_communication.agent_core.context import validate_closed_agent_task
            validate_closed_agent_task(projected)
        return projected

    def commit_projected_events_locked(self, conversation_id, record):
        """调用者须持续持有共享锁，只提交同锁内已经通过校验的投影。"""
        self._replace_locked(conversation_id, record)
        self._release_unavailable_locked(conversation_id, record.payload.get('input', {}).get('files', []))

    def accept_tool_locked(self, conversation_id, turn_id, item):
        record = self._record(conversation_id, turn_id)
        if record.status != 'processing':
            raise ValueError('只允许处理中的任务记录工具调用')
        self._replace_locked(conversation_id, record.model_copy(update={'payload': append_tool(record.payload, item)}))

    def accept_agent_exchange_locked(self, conversation_id, turn_id, messages):
        """只写入已接受的完整原生交互；与任务状态共用锁，不保存临时纠错。"""
        from app.agent.contract_communication.agent_core.native_messages import validate_native_messages
        record = self._record(conversation_id, turn_id)
        if record.status != 'processing' or not record.payload.get('agent_core_ready'):
            raise ValueError('仅门禁通过且仍在处理的任务可记录原生交互')
        batch = validate_native_messages(messages)
        if not batch or batch[0]['role'] != 'assistant':
            raise ValueError('原生交互批次必须从工具调用开始')
        payload = deepcopy(record.payload)
        previous = payload.get('agent_messages', [])
        call_id = batch[0]['tool_calls'][0]['id']
        for index, message in enumerate(previous):
            if message['role'] == 'assistant' and message['tool_calls'][0]['id'] == call_id:
                if previous[index:index + len(batch)] == batch:
                    return  # 已接受回执的重复保存无副作用。
                raise ValueError('调用标识已对应其他原生交互')
        payload['agent_messages'] = validate_native_messages([*previous, *batch])
        self._replace_locked(conversation_id, record.model_copy(update={'payload': payload}))

    async def insert_agent_summary(self, conversation_id, turn_id, *, expected_summary, summary, scope):
        """只修改驻留轨迹；与备份串行，防止在途快照携带旧序号。"""
        from app.agent.contract_communication.agent_core.subgraph.fifo_management.fifo_summary.schema import FIFOTopicSummary
        from dataclasses import replace
        value = FIFOTopicSummary.model_validate(summary)
        if value.latest_coverage != scope:
            raise ValueError('摘要覆盖范围与验收范围不一致')
        async with self._backup_lock, self._lock:
            _, previous, selected = self.get_agent_core_snapshot_locked(conversation_id, turn_id)
            if previous != expected_summary:
                raise ValueError('摘要基线已变化，拒绝迟到提交')
            ids = scope.task_ids
            prefix = selected[:len(ids)]
            if (not prefix or [r.record_id for r in prefix] != ids or prefix[-1].status != 'completed'
                    or prefix[0].record_id != scope.start_task_id or prefix[-1].record_id != scope.end_task_id
                    or any(r.turn_id == turn_id for r in prefix)):
                raise ValueError('摘要必须覆盖当前历史的完整已结束前缀')
            resident = self._resident[conversation_id]
            boundary = prefix[-1].sequence
            inserted = ConversationHistoryRecord(record_id=str(uuid4()), sequence=boundary + 1,
                kind='summary', turn_id=None, status=None, payload=value.model_dump(mode='json'),
                created_at=int(datetime.now(timezone.utc).timestamp() * 1000))
            records = []
            for record in resident.history.records:
                records.append(record.model_copy(update={'sequence': record.sequence + 1})
                               if record.sequence > boundary else record)
                if record.record_id == prefix[-1].record_id:
                    records.append(inserted)
            resident.history = resident.history.model_copy(update={
                'records': tuple(records), 'model_context_start_sequence': inserted.sequence})
            positions = {r.record_id: r.sequence for r in records}
            if resident.pending_backup is not None:
                backup = resident.pending_backup
                # 失败快照仍沿用原工作区版本；但排序与新摘要必须共同落盘，
                # 不能先重排旧任务、再等另一笔事务补摘要。
                pending = []
                for record in records:
                    if record.record_id in self._persisted:
                        continue
                    if record.kind == 'task' and record.status not in TERMINAL_STATUSES:
                        break
                    pending.append(record.model_copy(deep=True))
                resident.pending_backup = replace(backup, positions=positions,
                    records=tuple(pending))
            self._touch_locked(conversation_id)
            self._dirty.set()

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
                    if resident.ephemeral:
                        continue
                    if conversation_id is not None and cid != conversation_id:
                        continue
                    if resident.pending_backup is None:
                        records = []
                        for record in resident.history.records:
                            if record.record_id in self._persisted:
                                continue
                            if record.kind == 'task' and record.status not in TERMINAL_STATUSES:
                                break
                            records.append(record.model_copy(deep=True))
                        if not records and resident.workspace.revision == resident.persisted_workspace_revision:
                            continue
                        resident.pending_backup = ConversationBackup(
                            tuple(records), resident.workspace.model_copy(deep=True), resident.persisted_workspace_revision,
                            {r.record_id: r.sequence for r in resident.history.records},
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
                        expected_workspace_revision=backup.expected_workspace_revision, positions=backup.positions,
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
                        if resident.workspace.revision != resident.persisted_workspace_revision or any(
                            r.record_id not in self._persisted and
                            (r.kind == 'summary' or r.status in TERMINAL_STATUSES)
                            for r in resident.history.records
                        ):
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
            return self.update_workspace_locked(conversation_id, payload=validated, expected_revision=expected_revision)

    def update_workspace_locked(self, conversation_id, *, payload, expected_revision):
        """调用者持共享锁并完成归属/任务状态校验，成功回执之前更新唯一驻留副本。"""
        validated = WorkspacePayload.model_validate(payload).model_copy(deep=True)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('工作区版本必须为非负整数')
        resident = self._resident[conversation_id]
        if resident.workspace.revision != expected_revision:
            raise CommunicationStoreConflict('工作区版本已过期')
        resident.workspace = WorkspaceSnapshot(payload=validated, revision=expected_revision + 1,
            updated_at=int(datetime.now(timezone.utc).timestamp() * 1000))
        self._touch_locked(conversation_id)
        self._dirty.set()
        return resident.workspace.model_copy(deep=True)

    async def create_workspace_entry(
        self, conversation_id: str, *, secret_key: SecretStr, section: str,
        entry: dict | str, expected_revision: int,
    ) -> tuple[str, WorkspaceSnapshot]:
        """创建用户补充、已知信息或剩余方向；方向结论须通过收束接口生成。"""
        prefixes = {'task_constraints': 'supplement', 'known_information': 'info',
                    'remaining_directions': 'direction'}
        if section not in prefixes:
            raise ValueError('不允许直接创建该工作区条目')
        snapshot = await self._workspace_for_edit(conversation_id, secret_key, expected_revision)
        payload = snapshot.payload.model_dump()
        target = payload['task_constraints']['supplements'] if section == 'task_constraints' else payload[section]
        key = f'{prefixes[section]}_{uuid4().hex}'
        while key in target or key in payload['explored_directions']:
            key = f'{prefixes[section]}_{uuid4().hex}'
        target[key] = entry
        accepted = await self.update_workspace(conversation_id, secret_key=secret_key,
            payload=payload, expected_revision=expected_revision)
        return key, accepted

    async def _workspace_for_edit(self, conversation_id, secret_key, expected_revision):
        """编辑前读取深拷贝；最终提交仍再次比较版本，拒绝读取后的竞争更新。"""
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('工作区版本必须为非负整数')
        snapshot = await self.get_workspace(conversation_id, secret_key=secret_key)
        if snapshot.revision != expected_revision:
            raise CommunicationStoreConflict('工作区版本已过期')
        return snapshot

    async def update_workspace_field(
        self, conversation_id: str, *, secret_key: SecretStr, section: str, key: str,
        field: str | None, value: object, expected_revision: int,
    ) -> WorkspaceSnapshot:
        """修改一个已有字段；task_constraints 中 task 或补充键直接更新文本。"""
        if section not in ('task_constraints', 'known_information', 'explored_directions', 'remaining_directions'):
            raise ValueError('不允许修改该工作区分区')
        if not isinstance(key, str) or (field is not None and not isinstance(field, str)):
            raise ValueError('工作区键与字段必须为字符串')
        snapshot = await self._workspace_for_edit(conversation_id, secret_key, expected_revision)
        payload = snapshot.payload.model_dump()
        if section == 'task_constraints':
            if field is not None:
                raise ValueError('任务与补充直接按键更新，不接受子字段')
            target = payload[section] if key == 'task' else payload[section]['supplements']
            if key not in target:
                raise ValueError('待修改的用户补充不存在')
            target[key] = value
        else:
            if key not in payload[section] or field not in payload[section][key]:
                raise ValueError('待修改条目或字段不存在')
            payload[section][key][field] = value
        return await self.update_workspace(conversation_id, secret_key=secret_key,
            payload=payload, expected_revision=expected_revision)

    async def complete_workspace_direction(
        self, conversation_id: str, *, secret_key: SecretStr, direction_id: str,
        outcome: str, conclusion: str, information_ids: list[str], expected_revision: int,
    ) -> WorkspaceSnapshot:
        """同一提交将剩余方向转入已探索区，保留 ID；任一校验失败均不移除原方向。"""
        snapshot = await self._workspace_for_edit(conversation_id, secret_key, expected_revision)
        payload = snapshot.payload.model_dump()
        if not isinstance(direction_id, str) or direction_id not in payload['remaining_directions']:
            raise ValueError('待收束的剩余方向不存在')
        direction = payload['remaining_directions'].pop(direction_id)
        payload['explored_directions'][direction_id] = {
            'plan': direction['plan'], 'outcome': outcome, 'conclusion': conclusion,
            'information_ids': information_ids,
        }
        return await self.update_workspace(conversation_id, secret_key=secret_key,
            payload=payload, expected_revision=expected_revision)

    async def remove_workspace_entry(
        self, conversation_id: str, *, secret_key: SecretStr, section: str, key: str,
        expected_revision: int,
    ) -> WorkspaceSnapshot:
        """移除不再有效的工作区条目；信息引用必须仍有效，原始轨迹不受影响。"""
        if section not in ('task_constraints', 'known_information', 'explored_directions', 'remaining_directions'):
            raise ValueError('不允许移除该分区的记录')
        snapshot = await self._workspace_for_edit(conversation_id, secret_key, expected_revision)
        payload = snapshot.payload.model_dump()
        target = payload[section]['supplements'] if section == 'task_constraints' else payload[section]
        if not isinstance(key, str) or key not in target:
            raise ValueError('待移除条目不存在')
        del target[key]
        return await self.update_workspace(conversation_id, secret_key=secret_key,
            payload=payload, expected_revision=expected_revision)

    async def archive_candidates(self, *, interval: float, idle_limit: int, idle_seconds: float) -> tuple[ArchiveCandidate, ...]:
        """扫描只读取统一驻留数据；单调时钟防止重复扫描或墙钟调整提前触发驱逐。"""
        async with self._lock:
            now = self._clock()
            candidates = []
            for cid, resident in tuple(self._resident.items()):
                if resident.ephemeral:
                    # 临时结果最多空闲保留十五分钟，不执行任何落盘或向量化。
                    active = any(r.status not in TERMINAL_STATUSES for r in resident.history.records)
                    if not active and now - resident.last_activity >= 900:
                        if resident.memory_query_pool:
                            resident.memory_query_pool.close()
                        self._resident.pop(cid)
                        self._owners.pop(cid, None)
                        if self.evict_runtime:
                            self.evict_runtime(cid)
                    continue
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
                if resident.memory_query_pool:
                    resident.memory_query_pool.close()
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
                resident = self._resident.get(conversation_id)
                if resident is not None and resident.ephemeral:
                    if self._owners.get(conversation_id) != secret_key:
                        raise LookupError('会话不存在')
                else:
                    await run_in_threadpool(self._store.delete_conversation, conversation_id, secret_key=secret_key)
                current = self._resident.pop(conversation_id, None)
                if current and current.memory_query_pool:
                    current.memory_query_pool.close()
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
                    for resident in self._resident.values() if not resident.ephemeral
                ):
                    break
        async with self._lock:
            for resident in self._resident.values():
                if resident.memory_query_pool:
                    resident.memory_query_pool.close()
            self._resident.clear()
            self._persisted.clear()
            self._owners.clear()


    async def save_agent_reasoning_locked(self, conversation_id, turn_id, *, call_id, fields, tokens, max_rounds, max_tokens):
        """调用者持共享锁；只为已接受的响应追加思考，先落盘再发布内存窗口。"""
        record = self._record(conversation_id, turn_id)
        if record.status != 'processing' or not record.payload.get('agent_core_ready'):
            raise ValueError('当前任务不能追加思考')
        if not any(m.get('role') == 'assistant' and m['tool_calls'][0]['id'] == call_id
                   for m in record.payload.get('agent_messages', [])):
            raise ValueError('思考缺少原始已接受响应位置')
        resident = self._resident[conversation_id]
        original = resident.reasoning_window
        updated = original.append(task_id=record.record_id, call_id=call_id, fields=fields,
            tokens=tokens, max_rounds=max_rounds, max_tokens=max_tokens)
        if not resident.ephemeral:
            await run_in_threadpool(self._store.save_reasoning_window, conversation_id,
                secret_key=self._owners[conversation_id], window=updated, expected_position=original.next_position)
        resident.reasoning_window = updated
