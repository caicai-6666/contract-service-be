"""中间件平台登录与心跳的异步 HTTP 契约。"""
from typing import Literal
from datetime import datetime

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class MiddlewareSessionResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra='ignore')
    code: str = Field(pattern=r'^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$')
    expires_in_seconds: int = Field(gt=0, strict=True)
    heartbeat_interval_seconds: int = Field(gt=0, strict=True)

    @model_validator(mode='after')
    def valid_interval(self):
        if self.heartbeat_interval_seconds >= self.expires_in_seconds:
            raise ValueError('心跳间隔必须小于会话有效期')
        return self


class MiddlewareLoginResponse(MiddlewareSessionResponse):
    access_token: SecretStr = Field(min_length=1)
    token_type: Literal['bearer']


class MiddlewareClient:
    """不自动重试，不记录请求凭据与响应正文；调度交给会话服务。"""
    def __init__(self, base_url: str, *, timeout_seconds: float = 10, transport=None):
        self._http = httpx.AsyncClient(base_url=base_url.rstrip('/') + '/',
            timeout=timeout_seconds, follow_redirects=False, trust_env=False, transport=transport)

    async def _post(self, path, *, body, model, token=None):
        headers = {'Authorization': f'Bearer {token.get_secret_value()}'} if token else {}
        response = await self._http.post(path, json=body, headers=headers)
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError('中间件成功响应状态必须为 200')
        result = model.model_validate(response.json())
        if result.code != body['platform_code']:
            raise ValueError('中间件响应平台与请求不一致')
        return result

    async def login(self, platform_code: str, secret: SecretStr) -> MiddlewareLoginResponse:
        return await self._post('middleware-service/api/platform/login',
            body={'platform_code': platform_code, 'secret': secret.get_secret_value()},
            model=MiddlewareLoginResponse)

    async def heartbeat(self, platform_code: str, token: SecretStr) -> MiddlewareSessionResponse:
        return await self._post('middleware-service/api/platform/heartbeat',
            body={'platform_code': platform_code}, token=token, model=MiddlewareSessionResponse)

    async def publish_ingestion(self, *, token, fields, pdf_bytes, timeout_seconds):
        return await publish_ingestion_request(self, token=token, fields=fields,
            pdf_bytes=pdf_bytes, timeout_seconds=timeout_seconds)

    async def publish_deletion(self, *, token, fields, pdf_bytes, timeout_seconds):
        return await _publish_file_request(self, path='middleware-service/api/deletion-requests/publish',
            token=token, fields=fields, pdf_bytes=pdf_bytes, timeout_seconds=timeout_seconds,
            duplicate_message='该平台的 source_id 已有尚未确认的删除申请')

    async def pull_review_result(self, *, token):
        response = await self._http.post('middleware-service/api/ingestion-results/pull',
            headers={'Authorization': f'Bearer {token.get_secret_value()}'})
        response.raise_for_status()
        if response.status_code == 204:
            return None
        if response.status_code != 200:
            raise ValueError('审核反馈拉取必须返回 200 或 204')
        return MiddlewareReviewDelivery.model_validate(response.json())

    async def ack_review_result(self, *, token, message_id, offset):
        response = await self._http.post('middleware-service/api/ingestion-results/ack',
            headers={'Authorization': f'Bearer {token.get_secret_value()}'},
            json={'message_id': message_id})
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError('审核反馈确认必须返回 200')
        result = MiddlewareReviewAck.model_validate(response.json())
        if result.message_id != message_id or result.offset != offset:
            raise ValueError('审核反馈确认身份与当前消息不一致')
        return result

    async def close(self):
        await self._http.aclose()


class MiddlewarePublishFields(BaseModel):
    """仅发送接口约定的表单字段，不能用平台代号替代令牌鉴权。"""
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=False)
    name: str = Field(min_length=1, max_length=200)
    abstract: str = Field(min_length=1, max_length=10000)
    source_id: str = Field(min_length=1, max_length=128, description='数据在本平台的唯一 ID，合同使用 document_id。')
    note: str = Field(max_length=10000)
    reviewer: str = Field(min_length=1, max_length=200)
    file_extension: Literal['pdf'] = 'pdf'

    @model_validator(mode='after')
    def required_text(self):
        if any(not value.strip() for value in (self.name, self.abstract, self.source_id, self.reviewer)):
            raise ValueError('发布名称、摘要、源数据 ID 与审核员不得为空白')
        return self


class MiddlewareDeletionPublishFields(BaseModel):
    """删除送审表单：上传人与申请人来自本地快照，身份认证只使用令牌。"""
    model_config = ConfigDict(extra='forbid')
    source_id: str = Field(min_length=1, max_length=128)
    passport: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=200)
    abstract: str = Field(min_length=1, max_length=10000)
    uploader: str = Field(min_length=1, max_length=200)
    applicant: str = Field(min_length=1, max_length=200)
    note: str = Field(max_length=10000)
    file_extension: Literal['pdf'] = 'pdf'

    @model_validator(mode='after')
    def required_text(self):
        if any(not value.strip() for value in (
            self.source_id, self.passport, self.name, self.abstract, self.uploader, self.applicant)):
            raise ValueError('删除送审的身份、名称、摘要与人员不得为空白')
        return self


class MiddlewarePublishOutcome(BaseModel):
    state: Literal['published', 'retry', 'blocked', 'uncertain']
    message_id: str | None = None
    error_code: str | None = None
    unauthorized: bool = False


async def publish_ingestion_request(client: MiddlewareClient, *, token: SecretStr,
                                    fields: MiddlewarePublishFields, pdf_bytes: bytes,
                                    timeout_seconds: float) -> MiddlewarePublishOutcome:
    return await _publish_file_request(client, path='middleware-service/api/ingestion-requests/publish',
        token=token, fields=fields, pdf_bytes=pdf_bytes, timeout_seconds=timeout_seconds,
        duplicate_message='该平台的 source_id 已有尚未确认的入库请求')


async def _publish_file_request(client: MiddlewareClient, *, path: str, token: SecretStr,
                                fields: BaseModel, pdf_bytes: bytes,
                                timeout_seconds: float, duplicate_message: str) -> MiddlewarePublishOutcome:
    """单次上传，无隐式重试；确认丢失时不能推断未发布。"""
    import asyncio
    try:
        async with asyncio.timeout(timeout_seconds):
            response = await client._http.post(path,
                headers={'Authorization': f'Bearer {token.get_secret_value()}'},
                data=fields.model_dump(), files={'file': ('contract.pdf', pdf_bytes, 'application/pdf')},
                timeout=timeout_seconds)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        return MiddlewarePublishOutcome(state='retry', error_code='connection_unavailable')
    except (httpx.TransportError, TimeoutError):
        return MiddlewarePublishOutcome(state='uncertain', error_code='publish_transport_uncertain')
    try:
        data = response.json()
    except ValueError:
        data = None
    if response.status_code == 401:
        return MiddlewarePublishOutcome(state='retry', error_code='unauthorized', unauthorized=True)
    if response.status_code in (403, 422):
        return MiddlewarePublishOutcome(state='blocked', error_code=f'publish_http_{response.status_code}')
    if response.status_code == 500 and isinstance(data, dict) and data.get('detail') == '文件保存失败':
        return MiddlewarePublishOutcome(state='retry', error_code='file_save_failed')
    if response.status_code == 503 and isinstance(data, dict) and data.get('detail') == '消息服务暂不可用，请稍后重试':
        return MiddlewarePublishOutcome(state='retry', error_code='message_service_unavailable')
    candidate = data if response.status_code == 201 else data.get('detail') if isinstance(data, dict) else None
    message_id = candidate.get('message_id') if isinstance(candidate, dict) else None
    if (not isinstance(message_id, str) or not message_id.strip()
            or message_id != message_id.strip() or len(message_id)>128):
        message_id = None
    if response.status_code == 201 and isinstance(data, dict) and data.get('status') == 'published' and message_id:
        return MiddlewarePublishOutcome(state='published', message_id=message_id)
    if response.status_code == 409 and isinstance(candidate, dict):
        offset = candidate.get('offset')
        # 两个流各自校验重复响应，不能把另一类申请或普通冲突当成成功。
        if (message_id and candidate.get('message') == duplicate_message
                and 'offset' in candidate and (offset is None or type(offset) is int and offset>=0)):
            return MiddlewarePublishOutcome(state='published', message_id=message_id, error_code='duplicate_request')
    # 未知响应保留候选 ID 并标记不确定，后台按原 source_id 延迟重试。
    return MiddlewarePublishOutcome(state='uncertain', message_id=message_id, error_code='publish_unconfirmed')


class MiddlewareReviewMessage(BaseModel):
    """空字符串 passport 表示拒绝；缺失、null 和纯空白均不可猜测为拒绝。"""
    model_config = ConfigDict(frozen=True, extra='ignore')
    message_id: str = Field(min_length=1, max_length=128, strict=True)
    request_id: str = Field(min_length=1, strict=True)
    target_platform_code: str = Field(pattern=r'^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$')
    note: str = Field(strict=True)
    reviewer: str = Field(min_length=1, strict=True)
    passport: str = Field(strict=True)
    created_at: datetime

    @model_validator(mode='after')
    def valid_feedback(self):
        if any(not v.strip() for v in (self.message_id, self.request_id, self.reviewer)):
            raise ValueError('审核反馈身份不能为空白')
        if self.passport and self.passport != self.passport.strip():
            raise ValueError('passport 不得仅为空白或含首尾空白')
        if self.created_at.tzinfo is None:
            raise ValueError('审核时间必须包含时区')
        return self


class MiddlewareReviewDelivery(BaseModel):
    model_config = ConfigDict(frozen=True, extra='ignore')
    offset: int = Field(ge=0, strict=True)
    message: MiddlewareReviewMessage


class MiddlewareReviewAck(BaseModel):
    model_config = ConfigDict(frozen=True, extra='ignore')
    message_id: str = Field(min_length=1, max_length=128, strict=True)
    status: Literal['acknowledged']
    offset: int = Field(ge=0, strict=True)
