"""子平台领取、续租与单次回复；网络重试复用已生成结果。"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4
import httpx
from app.infrastructure.a2a import A2AClient
from app.service.a2a_response import A2AResponseService

logger=logging.getLogger(__name__)


@dataclass
class Execution:
    delivery: object
    task: asyncio.Task
    payload: dict | None = None
    uncertain: bool = False


class A2AWorker:
    def __init__(self, settings, session, *, metadata, es=None, relations=None,
                 field_catalog=None, category_catalog=None, client=None, service=None):
        self.settings, self.session = settings, session
        self.client=client or A2AClient(settings,session)
        self.service=service or A2AResponseService(settings, metadata=metadata,load_level=self.client.file_level,
            es=es,relations=relations,field_catalog=field_catalog,category_catalog=category_catalog)
        self.executions={}
        self.pending_pull=None
        self.runner=None

    async def start(self):
        if self.runner is None: self.runner=asyncio.create_task(self._run(),name='a2a-worker')

    async def _run(self):
        while True:
            try: await self.step()
            except asyncio.CancelledError: raise
            except Exception as exc:
                # 禁止输出消息、用户信息、等级、令牌及 HTTP 异常正文。
                logger.warning('A2A后台操作失败，将重试：error_type=%s',type(exc).__name__)
            await asyncio.sleep(self.settings.a2a_poll_interval_seconds)

    async def _retire(self, task_id):
        state=self.executions.pop(task_id,None)
        if state is not None:
            state.task.cancel()
            await self.service.abandon(task_id)
            await asyncio.gather(state.task,return_exceptions=True)

    async def step(self):
        for task_id,state in list(self.executions.items()):
            if not state.uncertain and state.delivery.lease_expires_at <= datetime.now(timezone.utc):
                await self._retire(task_id)
        if self.session.get_access_token() is None: return
        # 未知领取结果先按原幂等键与原参数恢复，避免发布使同批回放失效。
        if self.pending_pull is None:
            for task_id,state in list(self.executions.items()):
                if state.task.done():
                    reply=await self.service.review_reply(state.delivery.task.incoming(),state.task.result())
                    candidate={'task_id':task_id,'delivery_token':state.delivery.delivery_token,**reply.model_dump()}
                    if state.uncertain and state.payload != candidate:
                        # 先前发布是否成功未知，不能更改幂等载荷；停止旧凭证，等待重领。
                        await self._retire(task_id)
                        continue
                    if state.payload is None or not state.uncertain:
                        state.payload={'task_id':task_id,'delivery_token':state.delivery.delivery_token,
                            **reply.model_dump()}
                    try:
                        result=await self.client.publish(state.payload)
                        if (result.broadcast_id != state.delivery.task.broadcast_id
                                or result.reply_flow != state.delivery.task.reply_flow):
                            raise ValueError('回复回执归属错误')
                    except httpx.HTTPStatusError as exc:
                        code=exc.response.status_code
                        if code==409:
                            await self._retire(task_id)
                        elif code==429:
                            state.uncertain=False
                        else:
                            state.uncertain=True
                        continue
                    except Exception:
                        state.uncertain=True
                        continue
                    self.executions.pop(task_id)
            # 不确定发布可能已经完成，不加入续租；继续原载荷重试以取得完成回执。
            renew=[{'task_id':k,'delivery_token':v.delivery.delivery_token}
                for k,v in self.executions.items() if not v.uncertain]
            self.pending_pull=(str(uuid4()),renew)
        key,renew=self.pending_pull
        try:
            batch=await self.client.pull(key,renew)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code==409:
                # 整批续租失败无法识别失效项，保守停止旧执行，再用新键重领。
                for item in renew: await self._retire(item['task_id'])
                self.pending_pull=None
            raise
        except Exception:
            # 保留原请求。租约到期即终止生成，禁止无租约继续处理。
            for task_id,state in list(self.executions.items()):
                if not state.uncertain and state.delivery.lease_expires_at<=datetime.now(timezone.utc):
                    await self._retire(task_id)
            raise
        returned={d.task.task_id for d in batch.renewed_tasks}
        if returned != {r['task_id'] for r in renew}: raise ValueError('续租响应不完整')
        for delivery in (*batch.renewed_tasks,*batch.tasks):
            request=delivery.task.incoming()
            if (request.target_platform_code!=self.settings.middleware_platform_code
                    or request.source_platform_code!='knowledge-accumulation'):
                raise ValueError('交付平台不匹配')
            state=self.executions.get(request.task_id)
            if state is not None:
                if state.delivery.task!=delivery.task or state.delivery.delivery_token!=delivery.delivery_token:
                    await self._retire(request.task_id)
                    state=None
                else: state.delivery=delivery
            if state is None:
                if delivery.lease_expires_at<=datetime.now(timezone.utc): continue
                self.executions[request.task_id]=Execution(delivery,
                    asyncio.create_task(self.service.handle(request)))
        self.pending_pull=None

    async def close(self):
        if self.runner is not None:
            self.runner.cancel()
            await asyncio.gather(self.runner,return_exceptions=True)
            self.runner=None
        for task_id in list(self.executions): await self._retire(task_id)
        await self.service.close()
        await self.client.close()
