"""本地 vLLM OpenAI 兼容接口客户端。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Literal, Self

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
)

from app.core.config import MLLMSettings
from app.infrastructure.inference_metrics import (
    build_inference_request_metrics,
    observe_inference_request,
)
from app.infrastructure.vllm_media_reference import (
    get_vllm_media_reference_coordinator,
    is_vllm_media_cache_miss,
    strip_media_reference_metadata,
)


class MLLMRequestError(RuntimeError):
    """MLLM 请求不可恢复地无效。"""


class MLLMUnavailableError(RuntimeError):
    """MLLM 暂时不可用，调用方可以降级或重试。"""


ToolPlacement = Literal["before_task", "after_task"]


@dataclass(frozen=True, slots=True)
class MLLMCompletion:
    """节点观测模型调用所需的最小响应信息。"""

    response_id: str | None
    model: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True)
class MLLMToolCall:
    """模型生成的一次 OpenAI 兼容函数调用。"""

    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class MLLMToolCompletion:
    """工具调用轮次所需的响应、消息和用量。"""

    completion: MLLMCompletion
    assistant_message: dict[str, Any]
    tool_calls: tuple[MLLMToolCall, ...]


class MLLMClient:
    """管理单次工作流中的异步 vLLM HTTP 连接。"""

    def __init__(
        self,
        settings: MLLMSettings,
        *,
        cache_salt: str | None = None,
    ) -> None:
        if cache_salt is not None and not cache_salt.strip():
            raise ValueError("cache_salt 传入时不能为空字符串")
        self._settings = settings
        # 默认不设置盐，保持生产请求的既有缓存行为；隔离性能实验可以为
        # 不同对照组指定独立命名空间，避免先运行的组污染后运行的组。
        self._cache_salt = cache_salt
        self._client = AsyncOpenAI(
            # OpenAI SDK 要求提供 key；本地未启用鉴权时使用非敏感占位值。
            api_key=settings.api_key or "vllm-local",
            base_url=f"{settings.base_url.rstrip('/')}/",
            timeout=settings.timeout_seconds,
            max_retries=0,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._client.close()

    async def create_chat_completion(
        self,
        *,
        messages: list[dict[str, Any]],
        max_completion_tokens: int,
        min_tokens: int = 0,
        temperature: float = 0,
        enable_thinking: bool = False,
    ) -> MLLMCompletion:
        """调用 Chat Completions，并区分无效请求与暂时不可用。"""
        if self._settings.endpoint != "chat_completions":
            raise MLLMRequestError(
                f"不支持的 MLLM endpoint：{self._settings.endpoint}"
            )

        extra_body: dict[str, Any] = {
            "min_tokens": min_tokens,
            "chat_template_kwargs": {
                "enable_thinking": enable_thinking,
            },
        }
        if self._cache_salt is not None:
            extra_body["cache_salt"] = self._cache_salt

        started_at = datetime.now(UTC)
        request_started_at = perf_counter()
        try:
            response = await self._create_completion_with_media_references(
                model=self._settings.model,
                messages=messages,
                max_completion_tokens=max_completion_tokens,
                temperature=temperature,
                stream=False,
                extra_body=extra_body,
            )
        except (APITimeoutError, APIConnectionError) as exc:
            self._observe_failed_request(
                started_at=started_at,
                request_started_at=request_started_at,
                error=exc,
            )
            raise MLLMUnavailableError(f"MLLM 连接失败：{exc}") from exc
        except APIStatusError as exc:
            self._observe_failed_request(
                started_at=started_at,
                request_started_at=request_started_at,
                error=exc,
                status_code=exc.status_code,
            )
            if exc.status_code >= 500 or exc.status_code in {408, 409, 429}:
                raise MLLMUnavailableError(
                    f"MLLM 服务暂时不可用：HTTP {exc.status_code}"
                ) from exc
            detail = exc.response.text[:500]
            raise MLLMRequestError(
                f"MLLM 请求被拒绝：HTTP {exc.status_code}，{detail}"
            ) from exc

        self._observe_successful_request(
            started_at=started_at,
            request_started_at=request_started_at,
            response=response,
        )

        usage = response.usage
        prompt_details = (
            getattr(usage, "prompt_tokens_details", None) if usage else None
        )
        return MLLMCompletion(
            response_id=response.id,
            model=response.model,
            prompt_tokens=usage.prompt_tokens if usage else None,
            completion_tokens=usage.completion_tokens if usage else None,
            cached_tokens=(
                getattr(prompt_details, "cached_tokens", None)
                if prompt_details
                else None
            ),
            finish_reason=(
                str(response.choices[0].finish_reason)
                if response.choices
                and getattr(response.choices[0], "finish_reason", None) is not None
                else None
            ),
        )

    async def create_tool_chat_completion(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: str | dict[str, Any],
        max_completion_tokens: int,
        temperature: float,
        top_p: float,
        top_k: int,
        presence_penalty: float,
        repetition_penalty: float,
        seed: int,
        enable_thinking: bool = False,
        tool_placement: ToolPlacement | None = None,
    ) -> MLLMToolCompletion:
        """异步调用 strict function tools，并返回可继续追加的助手消息。"""
        if self._settings.endpoint != "chat_completions":
            raise MLLMRequestError(
                f"不支持的 MLLM endpoint：{self._settings.endpoint}"
            )
        if not tools:
            raise ValueError("工具调用请求至少需要一个工具")

        chat_template_kwargs: dict[str, Any] = {
            "enable_thinking": enable_thinking,
        }
        # 布局由节点契约决定；未指定时不覆盖服务端模板默认值，便于尚未
        # 完成消息边界设计的节点继续保持现状。
        if tool_placement is not None:
            chat_template_kwargs["tool_placement"] = tool_placement

        extra_body: dict[str, Any] = {
            "top_k": top_k,
            "repetition_penalty": repetition_penalty,
            "chat_template_kwargs": chat_template_kwargs,
        }
        if self._cache_salt is not None:
            extra_body["cache_salt"] = self._cache_salt

        started_at = datetime.now(UTC)
        request_started_at = perf_counter()
        try:
            response = await self._create_completion_with_media_references(
                model=self._settings.model,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                parallel_tool_calls=False,
                max_completion_tokens=max_completion_tokens,
                temperature=temperature,
                top_p=top_p,
                presence_penalty=presence_penalty,
                seed=seed,
                stream=False,
                extra_body=extra_body,
            )
        except (APITimeoutError, APIConnectionError) as exc:
            self._observe_failed_request(
                started_at=started_at,
                request_started_at=request_started_at,
                error=exc,
            )
            raise MLLMUnavailableError(f"MLLM 连接失败：{exc}") from exc
        except APIStatusError as exc:
            self._observe_failed_request(
                started_at=started_at,
                request_started_at=request_started_at,
                error=exc,
                status_code=exc.status_code,
            )
            if exc.status_code >= 500 or exc.status_code in {408, 409, 429}:
                raise MLLMUnavailableError(
                    f"MLLM 服务暂时不可用：HTTP {exc.status_code}"
                ) from exc
            detail = exc.response.text[:500]
            raise MLLMRequestError(
                f"MLLM 请求被拒绝：HTTP {exc.status_code}，{detail}"
            ) from exc

        self._observe_successful_request(
            started_at=started_at,
            request_started_at=request_started_at,
            response=response,
        )

        if not response.choices:
            raise MLLMRequestError("MLLM 工具调用响应不包含 choices")

        choice = response.choices[0]
        message = choice.message
        tool_calls: list[MLLMToolCall] = []
        assistant_tool_calls: list[dict[str, Any]] = []
        for call in message.tool_calls or ():
            if call.type != "function":
                raise MLLMRequestError(f"不支持的工具调用类型：{call.type}")
            tool_call = MLLMToolCall(
                call_id=call.id,
                name=call.function.name,
                arguments=call.function.arguments,
            )
            tool_calls.append(tool_call)
            assistant_tool_calls.append(
                {
                    "id": tool_call.call_id,
                    "type": "function",
                    "function": {
                        "name": tool_call.name,
                        "arguments": tool_call.arguments,
                    },
                }
            )

        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": message.content,
        }
        if assistant_tool_calls:
            assistant_message["tool_calls"] = assistant_tool_calls

        usage = response.usage
        prompt_details = (
            getattr(usage, "prompt_tokens_details", None) if usage else None
        )
        return MLLMToolCompletion(
            completion=MLLMCompletion(
                response_id=response.id,
                model=response.model,
                prompt_tokens=usage.prompt_tokens if usage else None,
                completion_tokens=usage.completion_tokens if usage else None,
                cached_tokens=(
                    getattr(prompt_details, "cached_tokens", None)
                    if prompt_details
                    else None
                ),
                finish_reason=(
                    str(choice.finish_reason)
                    if getattr(choice, "finish_reason", None) is not None
                    else None
                ),
            ),
            assistant_message=assistant_message,
            tool_calls=tuple(tool_calls),
        )

    async def _create_completion_with_media_references(
        self,
        *,
        messages: list[dict[str, Any]],
        **request: Any,
    ) -> Any:
        """首次发送完整媒体，后续只传 UUID；cache miss 时自动重填一次。"""
        if not self._settings.use_media_references:
            return await self._client.chat.completions.create(
                messages=strip_media_reference_metadata(messages),
                **request,
            )

        coordinator = get_vllm_media_reference_coordinator(
            base_url=self._settings.base_url,
            model=self._settings.model,
        )
        prepared = await coordinator.prepare(messages)
        try:
            response = await self._client.chat.completions.create(
                messages=prepared.messages,
                **request,
            )
        except APIStatusError as exc:
            await coordinator.finish(prepared, succeeded=False)
            if (
                not prepared.referenced_media_uuids
                or not is_vllm_media_cache_miss(exc)
            ):
                raise

            # vLLM 重启、负载均衡到另一 API worker 或 LRU 淘汰都会令
            # 本地 ready 状态过期。只对明确 cache miss 透明重填一次，
            # 其他 400 仍交给节点按原错误边界处理。
            await coordinator.invalidate(prepared.media_uuids)
            retry = await coordinator.prepare(messages)
            try:
                response = await self._client.chat.completions.create(
                    messages=retry.messages,
                    **request,
                )
            except BaseException:
                await coordinator.finish(retry, succeeded=False)
                raise
            await coordinator.finish(retry, succeeded=True)
            return response
        except BaseException:
            await coordinator.finish(prepared, succeeded=False)
            raise

        await coordinator.finish(prepared, succeeded=True)
        return response

    def _observe_successful_request(
        self,
        *,
        started_at: datetime,
        request_started_at: float,
        response: object,
    ) -> None:
        """把成功响应中的逐请求指标发送给任务局部观察器。"""
        observe_inference_request(
            build_inference_request_metrics(
                provider="mllm",
                endpoint=self._settings.endpoint,
                model=self._settings.model,
                started_at=started_at,
                elapsed_ms=(perf_counter() - request_started_at) * 1000,
                response=response,
                status_code=200,
            )
        )

    def _observe_failed_request(
        self,
        *,
        started_at: datetime,
        request_started_at: float,
        error: Exception,
        status_code: int | None = None,
    ) -> None:
        """记录不包含响应正文的失败请求观测。"""
        observe_inference_request(
            build_inference_request_metrics(
                provider="mllm",
                endpoint=self._settings.endpoint,
                model=self._settings.model,
                started_at=started_at,
                elapsed_ms=(perf_counter() - request_started_at) * 1000,
                error=error,
                status_code=status_code,
            )
        )
