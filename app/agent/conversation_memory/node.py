"""单任务确定性建模、三路编码与完整结果收束；不筛选任务、不写数据库。"""
import asyncio
from copy import deepcopy
import math
import operator
import re
from typing import Annotated, Literal
from typing_extensions import TypedDict
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schema.communication import ConversationHistoryRecord, TERMINAL_STATUSES
from app.agent.conversation_memory.embedding import embed_task_text
from app.agent.conversation_memory.prompt.embedding import EmbeddingContentKind
from app.schema.communication_retrieval import TaskRetrievalRecord
from app.core.config import get_settings

AREAS = ('user_input', 'intermediate_output', 'final_output')


class TaskMemoryModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    record_id: str
    user_input: str | None = None
    # 用户文字为一项，每份非空附件为一项；展示正文仍合并保存。
    user_input_parts: tuple[str, ...] = ()
    user_input_kinds: tuple[EmbeddingContentKind, ...] = ()
    intermediate_outputs: tuple[str, ...] = ()
    final_output: str | None = None


class TaskMemoryOutput(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    execution_status: Literal['completed', 'failed']
    record: ConversationHistoryRecord | None = None
    retrieval: TaskRetrievalRecord | None = None
    error: str | None = None
    audit: tuple[dict, ...] = ()

    @model_validator(mode='after')
    def complete_pair(self):
        if self.execution_status == 'completed':
            if self.record is None or self.retrieval is None or self.error is not None:
                raise ValueError('成功必须同时返回原任务及完整检索投影')
            if self.record.record_id != self.retrieval.record_id:
                raise ValueError('原任务与检索投影身份不一致')
            if self.record.kind != 'task' or self.record.status not in TERMINAL_STATUSES or not self.record.turn_id:
                raise ValueError('待入库原记录必须是终态任务')
        elif self.record is not None or self.retrieval is not None or not self.error:
            raise ValueError('失败不发布部分检索投影')
        return self


class TaskMemoryInputState(TypedDict):
    request: ConversationHistoryRecord


class TaskMemoryOutputState(TypedDict):
    execution_status: str
    record: ConversationHistoryRecord | None
    retrieval: TaskRetrievalRecord | None
    error: str | None
    audit: tuple[dict, ...]


class TaskMemoryState(TaskMemoryInputState, TaskMemoryOutputState):
    model: TaskMemoryModel
    branches: Annotated[list[dict], operator.add]


def _text(value):
    if value is None:
        return ''
    if not isinstance(value, str):
        raise ValueError('任务正文必须为字符串或空值')
    return value.strip()


def model_task(state):
    """只接收一个终态任务，重校验并复制，拒绝列表及上游伪造加工结果。"""
    raw = state['request']
    request = ConversationHistoryRecord.model_validate(raw.model_dump() if isinstance(raw, ConversationHistoryRecord) else raw)
    if request.kind != 'task' or request.status not in TERMINAL_STATUSES or not request.turn_id or not request.record_id.strip():
        raise ValueError('只接受具有稳定身份的终态原任务记录')
    request = request.model_copy(deep=True)
    payload = request.payload
    inp = payload.get('input', {})
    trace = payload.get('trace', [])
    if not isinstance(inp, dict) or not isinstance(trace, list):
        raise ValueError('任务input必须为对象，trace必须为列表')
    text = _text(inp.get('text'))
    text = re.sub(r'file_id\s*[:=]\s*(?:[0-9a-f]{64}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})(?![0-9a-f-])',
                  '', text, flags=re.IGNORECASE).strip()
    parts = [f'用户问题：{text}'] if text else []
    files = inp.get('files', [])
    if not isinstance(files, list):
        raise ValueError('附件必须为列表')
    contracts = inp.get('contracts', [])
    if not isinstance(contracts, list):
        raise ValueError('引用合同必须为列表')
    # 两类文件共用格式化和独立编码；合同 ID 仅留在权威任务，不参与检索投影。
    for file in [*files, *contracts]:
        if not isinstance(file, dict):
            raise ValueError('附件必须为对象')
        rows = []
        for key, label in [('file_name', '文件名'), ('display_name', '展示名称'), ('summary', '文件摘要')]:
            value = _text(file.get(key))
            if value:
                rows.append(f'{label}：{value}')
        if rows:
            parts.append('\n'.join(rows))
    # 公开trace是唯一来源；不重复读取agent_messages/events，不提取内部工具反馈或思考。
    messages = []
    grouped = {}
    for item in trace:
        if not isinstance(item, dict):
            raise ValueError('轨迹条目必须为对象')
        if item.get('type') != 'message' or item.get('message_kind') not in ('intermediate', 'final'):
            continue
        if item.get('status') != 'completed':
            continue
        content = item.get('text')
        if not _text(content):
            continue
        kind = item['message_kind']
        mid = item.get('message_id')
        # 同一公开消息可能因工具穿插拆片。按首次位置合并片段，不能把片段当多次输出。
        key = (kind, mid) if isinstance(mid, str) and mid else None
        if key is not None and key in grouped:
            messages[grouped[key]][1] += content
        else:
            if key is not None:
                grouped[key] = len(messages)
            messages.append([kind, content])
    model = TaskMemoryModel(record_id=request.record_id, user_input='\n\n'.join(parts) or None,
                           user_input_parts=tuple(parts),
                           user_input_kinds=(('user_question',) if text else ()) + ('file',) * (len(parts) - bool(text)),
                           intermediate_outputs=tuple(t.strip() for k,t in messages if k == 'intermediate'),
                           final_output='\n\n'.join(t.strip() for k,t in messages if k == 'final') or None)
    return {'request':request, 'model':model}


def active_branches(state):
    model = state['model']
    targets = []
    if model.user_input: targets.append('embed_user_input')
    if model.intermediate_outputs: targets.append('embed_intermediate_outputs')
    if model.final_output: targets.append('embed_final_output')
    return targets or ['collect_task_retrieval']


def fuse_vectors(vectors):
    """条内归一化、等权平均、再次归一化；不对多条反馈额外增加任务入口权重。"""
    if not vectors:
        raise ValueError('不能融合空向量列表')
    dimension = len(vectors[0])
    normalized = []
    for v in vectors:
        if len(v) != dimension or not all(math.isfinite(x) for x in v):
            raise ValueError('融合向量维度或数值非法')
        norm = math.hypot(*v)
        if norm <= 0: raise ValueError('融合向量不能为零')
        normalized.append(tuple(x / norm for x in v))
    mean = tuple(math.fsum(v[i] for v in normalized)/len(normalized) for i in range(dimension))
    norm = math.hypot(*mean)
    if norm <= 1e-12: raise ValueError('融合结果范数无效')
    return tuple(x/norm for x in mean)


async def _encode_area(area, texts, kinds=None):
    if kinds is not None and len(kinds) != len(texts):
        raise ValueError('编码类型与正文数量不一致')
    audits = [{} for _ in texts]
    semaphore = asyncio.Semaphore(get_settings().embedding.max_concurrent_requests)
    async def encode(index, text):
        async with semaphore:
            if kinds is not None:
                return await embed_task_text(text, audits[index], kind=kinds[index])
            return await embed_task_text(text, audits[index])
    # gather按输入顺序返回，某条失败仍等待其余作业清理，禁止发布部分区域结果。
    results = await asyncio.gather(*(encode(i,t) for i,t in enumerate(texts)), return_exceptions=True)
    for result in results:
        if isinstance(result, asyncio.CancelledError):
            raise result
    errors = [r for r in results if isinstance(r, BaseException)]
    if errors:
        return {'branches':[{'area':area, 'error':type(errors[0]).__name__, 'audit':audits}]}
    try:
        vector = fuse_vectors(results) if area in ('user_input', 'intermediate_output') else results[0]
        return {'branches':[{'area':area, 'vector':vector, 'error':None, 'audit':audits}]}
    except Exception as exc:
        return {'branches':[{'area':area, 'error':type(exc).__name__, 'audit':audits}]}


async def embed_user_input(state):
    return await _encode_area('user_input', state['model'].user_input_parts, state['model'].user_input_kinds)


async def embed_intermediate_outputs(state):
    return await _encode_area('intermediate_output', state['model'].intermediate_outputs)


async def embed_final_output(state):
    return await _encode_area('final_output', [state['model'].final_output], ['final_output'])


INTERMEDIATE_RENDER_VERSION = 'numbered-intermediate-v1'


def render_intermediate_outputs(texts):
    """格式化检索展示正文；编号仅用于展示，不参与逐条编码，也不是消息ID。"""
    if not texts:
        return None
    return '\n\n---\n\n'.join(f'### 中途输出 {index}\n\n{text}'
                                 for index, text in enumerate(texts, 1))


def collect_task_retrieval(state):
    """唯一收束点校验分支覆盖；任一区域失败，整个任务不返回半成品。"""
    model = state['model']
    texts = {'user_input':model.user_input,
             'intermediate_output':render_intermediate_outputs(model.intermediate_outputs),
             'final_output':model.final_output}
    expected = {area for area, text in texts.items() if text}
    branches = state.get('branches', [])
    by_area = {b['area']:b for b in branches}
    if len(by_area) != len(branches) or set(by_area) != expected:
        raise ValueError('编码分支覆盖不完整或重复')
    audit = tuple({'area':area, 'items':deepcopy(by_area[area]['audit'])} for area in AREAS if area in by_area)
    failures = [area for area in AREAS if area in by_area and by_area[area]['error']]
    if failures:
        return TaskMemoryOutput(execution_status='failed', error='向量化失败：'+', '.join(failures), audit=audit).model_dump()
    data = {'record_id':model.record_id}
    for area in AREAS:
        data[area+'_text'] = texts[area]
        data[area+'_embedding'] = by_area[area]['vector'] if area in by_area else None
    return TaskMemoryOutput(execution_status='completed', record=state['request'].model_copy(deep=True),
                            retrieval=TaskRetrievalRecord.model_validate(data),audit=audit).model_dump()
