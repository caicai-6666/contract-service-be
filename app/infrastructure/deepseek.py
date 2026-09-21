"""通过 AsyncOpenAI 调用 DeepSeek；连接可复用，消息历史由调用方持有。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Literal, Self

import httpx
from openai import APIConnectionError, APIResponseValidationError, APIStatusError, AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import DeepSeekSettings
from .inference_metrics import build_inference_request_metrics, observe_inference_request
from .model_concurrency import get_model_request_limiter


class DeepSeekRequestError(RuntimeError):
    """配置、输入、响应格式或不可重试的远端请求错误。"""


class DeepSeekUnavailableError(RuntimeError):
    """连接、超时、限流或服务端故障；是否重试由调用方决定。"""


class _TextMessage(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    role: Literal['system', 'user', 'assistant']
    content: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class DeepSeekCompletion:
    response_id: str
    model: str
    content: str
    finish_reason: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    raw_response: dict[str, Any]
    output_items: tuple[dict[str, Any], ...]

    @property
    def is_complete(self) -> bool:
        """截断结果仍供审计，但不能作为完整专家答复使用。"""
        return self.finish_reason == 'completed'


class DeepSeekClient:
    """无状态文字对话客户端；同一实例复用连接，支持 async with。"""

    def __init__(self, settings: DeepSeekSettings, *, http_client: httpx.AsyncClient | None = None):
        key = settings.api_key.get_secret_value() if settings.api_key else ''
        if not key.strip():
            raise DeepSeekRequestError('请配置 DEEPSEEK_API_KEY 后再创建专家客户端')
        self._settings = settings
        # 外部付费请求不隐式重试；注入 HTTP 客户端时，其关闭责任一并转移。
        # 兼容端不保证返回 OpenAI 新增的所有字段；使用 SDK 默认解析，下面校验业务必需字段。
        self._client = AsyncOpenAI(api_key=key, base_url=settings.base_url.rstrip('/') + '/',
                                   timeout=settings.timeout_seconds, max_retries=0,
                                   http_client=http_client)

    async def __aenter__(self) -> Self:
        if self._client.is_closed():
            raise DeepSeekRequestError('DeepSeek 客户端已关闭')
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def close(self) -> None:
        if not self._client.is_closed():
            await self._client.close()

    async def create_response(self, *, input_items: list[dict[str, Any]]) -> DeepSeekCompletion:
        """每轮开放内置搜索；显式携带文字消息及此前 Responses 输出项目。"""
        if self._client.is_closed():
            raise DeepSeekRequestError('DeepSeek 客户端已关闭')
        try:
            if not isinstance(input_items, list) or not input_items:
                raise ValueError('空输入')
            for item in input_items:
                if not isinstance(item, dict):
                    raise ValueError('输入项必须为对象')
                if item.get('type') in {'reasoning', 'web_search_call'}:
                    # 仅供程序回传服务端产物，不向主助手暴露自由填写历史的参数。
                    if not isinstance(item.get('id'), str) or not item['id']:
                        raise ValueError('历史输出缺少标识')
                elif item.get('type') == 'message' and item.get('role') == 'assistant':
                    parts = item.get('content')
                    if not isinstance(parts, list) or not parts or any(
                        p.get('type') != 'output_text' or not isinstance(p.get('text'), str) for p in parts
                    ):
                        raise ValueError('历史答复必须为文字输出')
                else:
                    message = _TextMessage.model_validate(item)
                    if not message.content.strip():
                        raise ValueError('空消息')
            validated = deepcopy(input_items)
        except (ValueError, TypeError, AttributeError) as exc:
            raise DeepSeekRequestError('输入必须为非空文字消息与受支持的历史输出项目') from exc
        started_at, started = datetime.now(UTC), perf_counter()
        response, error, status_code = None, None, None
        try:
            async with get_model_request_limiter('deepseek', self._settings.max_concurrent_requests):
                response = await self._client.responses.create(
                    model=self._settings.model,
                    input=validated,
                    max_output_tokens=self._settings.max_completion_tokens,
                    reasoning={'effort': self._settings.reasoning_effort},
                    tools=[{'type': 'web_search'}],
                    stream=False,
                )
            status_code = 200
            raw = response.model_dump(mode='json')
            if response.status not in {'completed', 'incomplete'}:
                raise DeepSeekRequestError('DeepSeek 未正常完成答复')
            output = raw.get('output', [])
            if any(item.get('type') not in {'message', 'reasoning', 'web_search_call'} for item in output):
                raise DeepSeekRequestError('DeepSeek 返回未支持的输出或客户端工具调用')
            for item in output:
                if item.get('type') == 'message' and any(p.get('type') != 'output_text' for p in item.get('content', [])):
                    raise DeepSeekRequestError('DeepSeek 未返回可用的文字答复')
            content = response.output_text
            if not content.strip() and response.status == 'completed':
                raise DeepSeekRequestError('DeepSeek 未返回有效答复正文')
            usage = response.usage
            return DeepSeekCompletion(
                response_id=response.id, model=response.model, content=content,
                finish_reason=response.status,
                prompt_tokens=usage.input_tokens if usage else None,
                completion_tokens=usage.output_tokens if usage else None,
                total_tokens=usage.total_tokens if usage else None,
                raw_response=raw, output_items=tuple(deepcopy(output)),
            )
        except APIStatusError as exc:
            error, status_code = exc, exc.status_code
            # 不将远端响应正文或包含凭据的请求对象带入面向模型的错误消息。
            error_type = DeepSeekUnavailableError if exc.status_code >= 500 or exc.status_code in {408, 409, 429} else DeepSeekRequestError
            raise error_type(f'DeepSeek 请求失败：HTTP {exc.status_code}') from exc
        except APIConnectionError as exc:
            error = exc
            raise DeepSeekUnavailableError('DeepSeek 连接失败或请求超时') from exc
        except APIResponseValidationError as exc:
            error = exc
            raise DeepSeekRequestError('DeepSeek 响应不符合接口契约') from exc
        except Exception as exc:
            error = exc
            raise
        finally:
            # 取消直接向上传播，既不重试也不发布成功指标。
            if response is not None or error is not None:
                observe_inference_request(build_inference_request_metrics(
                    provider='deepseek', endpoint='responses', model=self._settings.model,
                    started_at=started_at, elapsed_ms=(perf_counter() - started) * 1000,
                    response=response, error=error, status_code=status_code,
                ))
