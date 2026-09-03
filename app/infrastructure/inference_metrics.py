"""任务局部的模型请求指标观察能力。"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal

InferenceProvider = Literal["mllm", "embedding"]


@dataclass(frozen=True, slots=True)
class InferenceRequestMetrics:
    """不包含提示词与模型正文的单次推理请求观测。"""

    provider: InferenceProvider
    stage: str | None
    endpoint: str
    model: str
    started_at: str
    elapsed_ms: float
    succeeded: bool
    response_id: str | None
    response_model: str | None
    status_code: int | None
    error_type: str | None
    prompt_tokens: int | None
    cached_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    server_metrics: dict[str, int | float | None] | None

    def to_dict(self) -> dict[str, Any]:
        """转换为可直接写入 JSON 的普通对象。"""
        return asdict(self)


InferenceMetricsObserver = Callable[[InferenceRequestMetrics], None]

_observer: ContextVar[InferenceMetricsObserver | None] = ContextVar(
    "inference_metrics_observer",
    default=None,
)
_stage: ContextVar[str | None] = ContextVar(
    "inference_metrics_stage",
    default=None,
)


@contextmanager
def bind_inference_metrics_observer(
    observer: InferenceMetricsObserver | None,
) -> Iterator[None]:
    """为当前异步任务及其子任务绑定指标接收器。"""
    token = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)


@contextmanager
def bind_inference_stage(stage: str) -> Iterator[None]:
    """标记当前请求所属实验阶段，并安全传播到并发子任务。"""
    token = _stage.set(stage)
    try:
        yield
    finally:
        _stage.reset(token)


def observe_inference_request(metrics: InferenceRequestMetrics) -> None:
    """发送非权威观察事件；观察器故障不得中断合同提取。"""
    observer = _observer.get()
    if observer is None:
        return
    try:
        observer(metrics)
    except Exception:
        # 指标采集属于旁路能力，不能反向改变模型请求的业务结果。
        return


def build_inference_request_metrics(
    *,
    provider: InferenceProvider,
    endpoint: str,
    model: str,
    started_at: datetime,
    elapsed_ms: float,
    response: Any | None = None,
    error: Exception | None = None,
    status_code: int | None = None,
) -> InferenceRequestMetrics:
    """从 OpenAI 兼容响应构造稳定、无正文的观测记录。"""
    usage = getattr(response, "usage", None) if response is not None else None
    prompt_details = (
        getattr(usage, "prompt_tokens_details", None)
        if usage is not None
        else None
    )
    model_extra = (
        getattr(response, "model_extra", None) if response is not None else None
    )
    raw_server_metrics = (
        model_extra.get("metrics")
        if isinstance(model_extra, Mapping)
        else None
    )
    server_metrics = _normalize_server_metrics(raw_server_metrics)
    return InferenceRequestMetrics(
        provider=provider,
        stage=_stage.get(),
        endpoint=endpoint,
        model=model,
        started_at=started_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        elapsed_ms=round(elapsed_ms, 3),
        succeeded=response is not None and error is None,
        response_id=(
            getattr(response, "id", None) if response is not None else None
        ),
        response_model=(
            getattr(response, "model", None) if response is not None else None
        ),
        status_code=status_code,
        error_type=type(error).__name__ if error is not None else None,
        prompt_tokens=(
            getattr(usage, "prompt_tokens", None) if usage is not None else None
        ),
        cached_tokens=(
            getattr(prompt_details, "cached_tokens", None)
            if prompt_details is not None
            else None
        ),
        completion_tokens=(
            getattr(usage, "completion_tokens", None)
            if usage is not None
            else None
        ),
        total_tokens=(
            getattr(usage, "total_tokens", None) if usage is not None else None
        ),
        server_metrics=server_metrics,
    )


def _normalize_server_metrics(
    value: object,
) -> dict[str, int | float | None] | None:
    """只保留 vLLM 指标对象中的数值字段，拒绝意外正文进入产物。"""
    if not isinstance(value, Mapping):
        return None
    normalized = {
        str(name): metric
        for name, metric in value.items()
        if metric is None
        or (
            isinstance(metric, (int, float))
            and not isinstance(metric, bool)
        )
    }
    return normalized or None


__all__ = [
    "InferenceRequestMetrics",
    "bind_inference_metrics_observer",
    "bind_inference_stage",
    "build_inference_request_metrics",
    "observe_inference_request",
]
