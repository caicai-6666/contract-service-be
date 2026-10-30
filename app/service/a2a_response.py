"""内存响应链：首次只受理，后续按响应ID接续；不经过门禁、不落盘。"""
import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from time import monotonic
from uuid import uuid4
from copy import deepcopy
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from app.agent.contract_communication.a2a import A2ATaskRequest, run_a2a_task, InMemoryA2ATask
from app.agent.contract_communication.a2a.access import ContractAccess, A2AContextExpired
from app.agent.contract_communication.a2a.contracts import A2AContractTools
from app.agent.contract_communication.a2a.admission import decide_admission
from app.infrastructure.mllm import MLLMClient
from app.schema.communication import ConversationHistoryRecord


class IncomingA2ATask(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    task_id: str = Field(min_length=1,max_length=128)
    userid: str = Field(min_length=1,max_length=128)
    source_platform_code: str = Field(min_length=1)
    target_platform_code: str = Field(min_length=1)
    message: str = Field(min_length=1,max_length=10000,pattern=r'\S')
    response_id: str | None = None
    created_at_ms: int = Field(ge=0)


class A2AReply(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    status: Literal['succeeded','not_applicable','context_expired','failed']
    message: str = Field(min_length=1,max_length=10000)
    response_id: str | None = None


@dataclass(frozen=True)
class ResponseEntry:
    userid: str
    source: str
    records: tuple
    workspace: object
    visible_ids: frozenset
    audit: tuple
    lock: asyncio.Lock
    touched: float


class A2AResponseService:
    def __init__(self, settings, *, metadata, load_level, es=None, relations=None,
                 field_catalog=None, category_catalog=None, client=None, admission_client=None, loop_options=None, clock=monotonic):
        self.settings, self.metadata, self.load_level = settings, metadata, load_level
        self.es, self.relations = es, relations
        self.field_catalog, self.category_catalog = field_catalog, category_catalog
        self.client, self.loop_options = client, dict(loop_options or {})
        # 认领与正式执行分开注入客户端，避免复用绑定 xhigh 的客户端覆盖 low 配置。
        self.admission_client = admission_client
        self.clock, self.responses, self.tasks = clock, OrderedDict(), OrderedDict()
        self.closed = False
        self.audits = OrderedDict()

    def _parent(self, task):
        entry = self.responses.get(task.response_id)
        if (entry is None or self.clock()-entry.touched >= self.settings.a2a_context_ttl_seconds
                or entry.userid != task.userid or entry.source != task.source_platform_code):
            raise A2AContextExpired('上下文不可用')
        self.responses.move_to_end(task.response_id)
        return entry

    async def handle(self, task):
        if self.closed: raise RuntimeError('A2A服务已关闭')
        task = IncomingA2ATask.model_validate(task)
        if task.target_platform_code != self.settings.middleware_platform_code or task.source_platform_code != 'knowledge-accumulation':
            raise ValueError('任务平台归属错误')
        for response_id,entry in list(self.responses.items()):
            if self.clock()-entry.touched >= self.settings.a2a_context_ttl_seconds:
                self.responses.pop(response_id,None)
        fingerprint = task.model_dump_json()
        previous = self.tasks.get(task.task_id)
        if previous is not None:
            if previous[0] != fingerprint: raise ValueError('同一任务标识收到冲突内容')
            return await self.review_reply(task, await asyncio.shield(previous[1]))
        # 在进程内为重领与不确定交付保留同一业务结果，绝不因网络重试重跑模型。
        while len(self.tasks) >= self.settings.a2a_receipt_capacity:
            old = next((k for k,v in self.tasks.items() if v[1].done()), None)
            if old is None: raise RuntimeError('A2A任务容量已满')
            self.tasks.pop(old)
        execution = asyncio.create_task(self._process(task))
        self.tasks[task.task_id] = (fingerprint, execution)
        return await asyncio.shield(execution)

    async def _process(self, task):
        audit = []
        try:
            parent = self._parent(task) if task.response_id is not None else None
            lock = parent.lock if parent is not None else asyncio.Lock()
            async with lock:
                if parent is not None:
                    # 等锁期间可能过期或被驱逐，再确认，不恢复已经失效的响应。
                    parent = self._parent(task)
                request = A2ATaskRequest(task_id=task.task_id, content=task.message, created_at=task.created_at_ms,
                    **({'workspace':parent.workspace} if parent is not None else {}))
                if parent is None:
                    admission_settings = self.settings.mllm.for_a2a_admission()
                    if self.admission_client is not None:
                        accepted, exchanges = await decide_admission(task.message,client=self.admission_client,settings=admission_settings,
                            audit=audit,tool_call_template=self.loop_options.get('tool_call_template'),
                            chat_counter=self.loop_options.get('chat_counter'))
                    else:
                        async with MLLMClient(admission_settings) as model:
                            accepted, exchanges = await decide_admission(task.message,client=model,settings=admission_settings,audit=audit,
                                tool_call_template=self.loop_options.get('tool_call_template'),
                                chat_counter=self.loop_options.get('chat_counter'))
                    if not accepted.accepted:
                        return A2AReply(status='not_applicable',message=accepted.message)
                    message, workspace, seen = accepted.message, request.workspace, frozenset()
                else:
                    access = ContractAccess(userid=task.userid,platform_code=task.target_platform_code,
                        load_level=self.load_level,metadata=self.metadata,previous_ids=parent.visible_ids)
                    try:
                        await access.refresh()
                    except A2AContextExpired:
                        self._invalidate(parent.lock)
                        raise
                    tools = A2AContractTools(access=access,settings=self.settings,es=self.es,relations=self.relations,
                        field_catalog=self.field_catalog,category_catalog=self.category_catalog,audit=audit)
                    host = InMemoryA2ATask(request,history=parent.records,authorize=access.refresh)
                    try:
                        result = await run_a2a_task(request,host=host,settings=self.settings.mllm,client=self.client,audit=audit,
                            additional_tools=tools.registrations(),page_resolver=tools.resolve_page,**self.loop_options)
                        await access.refresh()
                        message, exchanges, workspace = result.content, host.exchanges, host.workspace
                        seen = frozenset(access.seen_ids)
                    except Exception:
                        if access.invalidated:
                            self._invalidate(parent.lock)
                            raise A2AContextExpired('上下文不可用')
                        raise
                    finally:
                        await tools.close()
                record = ConversationHistoryRecord(record_id=task.task_id,sequence=len(parent.records)+1 if parent else 1,
                    kind='task',turn_id=task.task_id,status='completed',created_at=task.created_at_ms,
                    payload={'input':{'text':task.message},'agent_messages':deepcopy(exchanges),'trace':[{
                        'type':'message','message_id':str(uuid4()),'message_kind':'final','text':message,'status':'completed','references':[]}]})
                response_id = str(uuid4())
                self.responses[response_id] = ResponseEntry(task.userid,task.source_platform_code,
                    (*parent.records,record) if parent else (record,),workspace.model_copy(deep=True),seen,
                    (*parent.audit,*audit) if parent else tuple(audit),lock,self.clock())
                # 整份响应检查点一起淘汰；活跃执行持有副本，但不可复活已失效引用。
                while len(self.responses)>self.settings.a2a_context_capacity: self.responses.popitem(last=False)
                return A2AReply(status='succeeded',message=message,response_id=response_id)
        except A2AContextExpired:
            return A2AReply(status='context_expired',message='历史上下文已不可用，请重新发起请求。')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            audit.append({'stage':'failure','error_type':type(exc).__name__})
            return A2AReply(status='failed',message='本次任务未能完成，请稍后重试或重新发起请求。')

        finally:
            self.audits[task.task_id] = tuple(deepcopy(audit))
            while len(self.audits) > self.settings.a2a_receipt_capacity:
                self.audits.popitem(last=False)

    async def review_reply(self, task, reply):
        """重领或交付重试也必须重新校验，禁止缓存的旧答案绕过权限变更。"""
        if reply.status != 'succeeded' or reply.response_id is None:
            return reply
        entry = self.responses.get(reply.response_id)
        if entry is None or self.clock()-entry.touched >= self.settings.a2a_context_ttl_seconds:
            return A2AReply(status='context_expired', message='历史上下文已不可用，请重新发起请求。')
        if entry.userid != task.userid or entry.source != task.source_platform_code:
            return A2AReply(status='context_expired', message='历史上下文已不可用，请重新发起请求。')
        if entry.visible_ids:
            access = ContractAccess(userid=task.userid, platform_code=task.target_platform_code,
                load_level=self.load_level, metadata=self.metadata, previous_ids=entry.visible_ids)
            try:
                await access.refresh()
            except A2AContextExpired:
                self._invalidate(entry.lock)
                return A2AReply(status='context_expired', message='历史上下文已不可用，请重新发起请求。')
            except Exception:
                return A2AReply(status='failed', message='本次任务未能完成，请稍后重试或重新发起请求。')
        return reply

    def _invalidate(self, lock):
        # 同一推理链的旧检查点也不可因之后权限恢复而复活。
        for key,entry in list(self.responses.items()):
            if entry.lock is lock: self.responses.pop(key,None)

    async def abandon(self, task_id):
        entry = self.tasks.get(task_id)
        if entry is not None and not entry[1].done():
            entry[1].cancel()
            await asyncio.gather(entry[1], return_exceptions=True)
            self.tasks.pop(task_id, None)

    async def close(self):
        self.closed = True
        tasks = [v[1] for v in self.tasks.values() if not v[1].done()]
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        self.tasks.clear();self.responses.clear();self.audits.clear()
