"""本地 vLLM OpenAI 兼容 Embedding 客户端。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Self

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
)
from openai.types import CreateEmbeddingResponse

from app.core.config import EmbeddingSettings
from app.infrastructure.inference_metrics import (
    build_inference_request_metrics,
    observe_inference_request,
)


class EmbeddingRequestError(RuntimeError):
    """Embedding 请求参数或响应不可恢复地无效。"""


class EmbeddingUnavailableError(RuntimeError):
    """Embedding 服务暂时不可用，调用方可以隔离失败批次。"""


@dataclass(frozen=True, slots=True)
class EmbeddingCompletion:
    """一次批量向量化响应及其最小观测信息。"""

    model: str | None
    vectors: tuple[tuple[float, ...], ...]
    prompt_tokens: int | None


class EmbeddingClient:
    """管理一次工作流中的异步 vLLM Embedding HTTP 连接。"""

    def __init__(self, settings: EmbeddingSettings) -> None:
        self._settings = settings
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

    async def create_embeddings(
        self,
        *,
        inputs: list[str],
    ) -> EmbeddingCompletion:
        """按输入顺序返回批量向量，并区分请求错误与服务不可用。"""
        if self._settings.endpoint != "embeddings":
            raise EmbeddingRequestError(
                f"不支持的 Embedding endpoint：{self._settings.endpoint}"
            )
        if not inputs:
            raise ValueError("Embedding 批量请求至少需要一个输入")

        started_at = datetime.now(UTC)
        request_started_at = perf_counter()
        try:
            response = await self._client.embeddings.create(
                model=self._settings.model,
                input=inputs,
                encoding_format="float",
            )
        except (APITimeoutError, APIConnectionError) as exc:
            observe_inference_request(
                build_inference_request_metrics(
                    provider="embedding",
                    endpoint=self._settings.endpoint,
                    model=self._settings.model,
                    started_at=started_at,
                    elapsed_ms=(perf_counter() - request_started_at) * 1000,
                    error=exc,
                )
            )
            raise EmbeddingUnavailableError(f"Embedding 连接失败：{exc}") from exc
        except APIStatusError as exc:
            observe_inference_request(
                build_inference_request_metrics(
                    provider="embedding",
                    endpoint=self._settings.endpoint,
                    model=self._settings.model,
                    started_at=started_at,
                    elapsed_ms=(perf_counter() - request_started_at) * 1000,
                    error=exc,
                    status_code=exc.status_code,
                )
            )
            if exc.status_code >= 500 or exc.status_code in {408, 409, 429}:
                raise EmbeddingUnavailableError(
                    f"Embedding 服务暂时不可用：HTTP {exc.status_code}"
                ) from exc
            detail = exc.response.text[:500]
            raise EmbeddingRequestError(
                f"Embedding 请求被拒绝：HTTP {exc.status_code}，{detail}"
            ) from exc

        observe_inference_request(
            build_inference_request_metrics(
                provider="embedding",
                endpoint=self._settings.endpoint,
                model=self._settings.model,
                started_at=started_at,
                elapsed_ms=(perf_counter() - request_started_at) * 1000,
                response=response,
                status_code=200,
            )
        )

        ordered = sorted(response.data, key=lambda item: item.index)
        if len(ordered) != len(inputs):
            raise EmbeddingRequestError(
                "Embedding 响应向量数量与输入数量不一致："
                f"expected={len(inputs)}, actual={len(ordered)}"
            )
        indexes = [item.index for item in ordered]
        if indexes != list(range(len(inputs))):
            raise EmbeddingRequestError(f"Embedding 响应索引不连续：{indexes}")

        usage = response.usage
        return EmbeddingCompletion(
            model=response.model,
            vectors=tuple(tuple(item.embedding) for item in ordered),
            prompt_tokens=usage.prompt_tokens if usage is not None else None,
        )

    async def create_multimodal_embedding(
        self,
        *,
        messages: list[dict[str, object]],
    ) -> EmbeddingCompletion:
        """调用 vLLM 多模态 Embedding 扩展，一次只编码一组页面消息。"""
        if self._settings.endpoint != "embeddings":
            raise EmbeddingRequestError(
                f"不支持的 Embedding endpoint：{self._settings.endpoint}"
            )
        started_at = datetime.now(UTC)
        request_started_at = perf_counter()
        try:
            response = await self._client.post(
                "/embeddings",
                cast_to=CreateEmbeddingResponse,
                body={
                    "messages": messages,
                    "model": self._settings.model,
                    "encoding_format": "float",
                    "continue_final_message": True,
                    "add_special_tokens": True,
                },
            )
        except (APITimeoutError, APIConnectionError) as exc:
            observe_inference_request(
                build_inference_request_metrics(
                    provider="embedding",
                    endpoint=self._settings.endpoint,
                    model=self._settings.model,
                    started_at=started_at,
                    elapsed_ms=(perf_counter() - request_started_at) * 1000,
                    error=exc,
                )
            )
            raise EmbeddingUnavailableError(f"Embedding 连接失败：{exc}") from exc
        except APIStatusError as exc:
            observe_inference_request(
                build_inference_request_metrics(
                    provider="embedding",
                    endpoint=self._settings.endpoint,
                    model=self._settings.model,
                    started_at=started_at,
                    elapsed_ms=(perf_counter() - request_started_at) * 1000,
                    error=exc,
                    status_code=exc.status_code,
                )
            )
            if exc.status_code >= 500 or exc.status_code in {408, 409, 429}:
                raise EmbeddingUnavailableError(
                    f"Embedding 服务暂时不可用：HTTP {exc.status_code}"
                ) from exc
            raise EmbeddingRequestError(
                f"Embedding 请求被拒绝：HTTP {exc.status_code}，"
                f"{exc.response.text[:500]}"
            ) from exc

        observe_inference_request(
            build_inference_request_metrics(
                provider="embedding",
                endpoint=self._settings.endpoint,
                model=self._settings.model,
                started_at=started_at,
                elapsed_ms=(perf_counter() - request_started_at) * 1000,
                response=response,
                status_code=200,
            )
        )
        if len(response.data) != 1:
            raise EmbeddingRequestError(
                "单页多模态 Embedding 必须返回一个向量："
                f"actual={len(response.data)}"
            )
        usage = response.usage
        return EmbeddingCompletion(
            model=response.model,
            vectors=(tuple(response.data[0].embedding),),
            prompt_tokens=usage.prompt_tokens if usage is not None else None,
        )


__all__ = [
    "EmbeddingClient",
    "EmbeddingCompletion",
    "EmbeddingRequestError",
    "EmbeddingUnavailableError",
]
