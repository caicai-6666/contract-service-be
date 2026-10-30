"""子平台 A2A HTTP 适配；交付凭证与等级只在服务端传递。"""
from datetime import datetime
from typing import Literal
import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.service.a2a_response import IncomingA2ATask


class FilePermission(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    file_level: int | None = Field(strict=True, ge=0)


class WireTask(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    task_id: str = Field(min_length=1, max_length=128)
    broadcast_id: str = Field(min_length=1, max_length=128)
    userid: str = Field(min_length=1, max_length=128)
    source_platform_code: str
    target_platform_code: str
    message: str = Field(min_length=1, max_length=10000)
    response_id: str | None
    response_platform_code: str | None
    created_at: datetime
    sequence: str = Field(pattern=r'^[0-9]+$')
    reply_flow: Literal['broadcast','direct']

    @model_validator(mode='after')
    def validate_route(self):
        if self.created_at.utcoffset() is None:
            raise ValueError('任务时间缺少时区')
        if self.response_id is None:
            if self.response_platform_code is not None or self.reply_flow != 'broadcast':
                raise ValueError('首次请求路由不一致')
        elif self.response_platform_code != self.target_platform_code or self.reply_flow != 'direct':
            raise ValueError('续聊路由不一致')
        return self

    def incoming(self):
        return IncomingA2ATask(**self.model_dump(include={'task_id','userid','source_platform_code',
            'target_platform_code','message','response_id'}), created_at_ms=int(self.created_at.timestamp()*1000))


class Delivery(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    task: WireTask
    delivery_token: str = Field(min_length=1, max_length=128, repr=False)
    lease_expires_at: datetime

    @model_validator(mode='after')
    def valid_time(self):
        if self.lease_expires_at.utcoffset() is None: raise ValueError('租约时间缺少时区')
        return self


class PullResult(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    duplicate: bool = Field(strict=True)
    capacity: int = Field(strict=True, ge=1, le=100)
    inflight_count: int = Field(strict=True, ge=0)
    available_slots: int = Field(strict=True, ge=0)
    tasks: list[Delivery]
    renewed_tasks: list[Delivery]

    @model_validator(mode='after')
    def valid_window(self):
        ids=[d.task.task_id for d in (*self.tasks,*self.renewed_tasks)]
        if len(ids)!=len(set(ids)) or self.inflight_count+self.available_slots != self.capacity:
            raise ValueError('任务窗口响应不一致')
        return self


class PublishResult(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    status: Literal['published']
    duplicate: bool = Field(strict=True)
    task_id: str
    broadcast_id: str
    message_id: str
    reply_flow: Literal['broadcast','direct']
    request_status: Literal['completed']
    reply_disposition: Literal['saved','discarded']
    completed_at: datetime


class A2AClient:
    def __init__(self, settings, session, *, transport=None):
        self.session = session
        self.http = httpx.AsyncClient(base_url=str(settings.middleware_base_url).rstrip('/')+'/',
            timeout=settings.middleware_request_timeout_seconds, follow_redirects=False, trust_env=False, transport=transport)

    async def _post(self, path, body, model, *, status=200, key=None):
        token=self.session.get_access_token()
        if token is None: raise RuntimeError('平台连接尚未就绪')
        headers={'Authorization':'Bearer '+token.get_secret_value()}
        if key is not None: headers['Idempotency-Key']=key
        response=await self.http.post('middleware-service/api/'+path, json=body, headers=headers)
        if response.status_code==401: self.session.invalidate_access_token(token)
        response.raise_for_status()
        if response.status_code != status: raise ValueError('中间件响应状态异常')
        return model.model_validate(response.json())

    async def file_level(self, userid):
        # 每次按 userid 实时读取，不传平台代码、不查询登录、不缓存管理员豁免。
        return (await self._post('user/file-permission', {'userid':userid}, FilePermission)).file_level

    async def pull(self, key, renew):
        return await self._post('platform-a2a/requests/pull', {'renew_tasks':renew}, PullResult, key=key)

    async def publish(self, payload):
        result=await self._post('platform-a2a/replies/publish', payload, PublishResult, status=201)
        if result.task_id != payload['task_id']: raise ValueError('回复回执任务不一致')
        return result

    async def close(self): await self.http.aclose()
