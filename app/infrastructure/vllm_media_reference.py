"""vLLM 多模态 UUID 引用的并发填充与缓存失效协调。"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Literal
from weakref import WeakKeyDictionary

_MediaState = Literal["seeding", "ready"]


@dataclass(frozen=True, slots=True)
class VLLMMediaReferenceRequest:
    """一次请求实际发送的消息和对应的媒体缓存状态。"""

    messages: list[dict[str, Any]]
    media_uuids: tuple[str, ...]
    claimed_media_uuids: tuple[str, ...]
    referenced_media_uuids: tuple[str, ...]


class VLLMMediaReferenceCoordinator:
    """保证同一媒体只由一个并发请求携带完整数据完成首次填充。"""

    def __init__(self, *, maximum_ready_media: int = 4096) -> None:
        if maximum_ready_media <= 0:
            raise ValueError("maximum_ready_media 必须大于 0")
        self._condition = asyncio.Condition()
        self._states: dict[str, _MediaState] = {}
        self._ready_order: OrderedDict[str, None] = OrderedDict()
        self._maximum_ready_media = maximum_ready_media

    async def prepare(
        self,
        messages: list[dict[str, Any]],
    ) -> VLLMMediaReferenceRequest:
        """等待重叠填充结束，并把已缓存媒体替换为仅 UUID 引用。"""
        media = _collect_media(messages)
        media_uuids = tuple(media)
        if not media_uuids:
            return VLLMMediaReferenceRequest(
                messages=messages,
                media_uuids=(),
                claimed_media_uuids=(),
                referenced_media_uuids=(),
            )

        async with self._condition:
            await self._condition.wait_for(
                lambda: not any(
                    self._states.get(media_uuid) == "seeding"
                    for media_uuid in media_uuids
                )
            )
            referenced = tuple(
                media_uuid
                for media_uuid in media_uuids
                if self._states.get(media_uuid) == "ready"
            )
            for media_uuid in referenced:
                self._ready_order.move_to_end(media_uuid)
            claimed = tuple(
                media_uuid
                for media_uuid in media_uuids
                if media_uuid not in self._states
            )
            for media_uuid in claimed:
                self._states[media_uuid] = "seeding"

        return VLLMMediaReferenceRequest(
            messages=_replace_media_with_references(messages, set(referenced)),
            media_uuids=media_uuids,
            claimed_media_uuids=claimed,
            referenced_media_uuids=referenced,
        )

    async def finish(
        self,
        request: VLLMMediaReferenceRequest,
        *,
        succeeded: bool,
    ) -> None:
        """成功后发布引用，失败或取消时允许其他请求重新填充。"""
        if not request.claimed_media_uuids:
            return
        async with self._condition:
            for media_uuid in request.claimed_media_uuids:
                if self._states.get(media_uuid) != "seeding":
                    continue
                if succeeded:
                    self._states[media_uuid] = "ready"
                    self._ready_order[media_uuid] = None
                    self._ready_order.move_to_end(media_uuid)
                else:
                    self._states.pop(media_uuid, None)
                    self._ready_order.pop(media_uuid, None)
            self._trim_ready_media()
            self._condition.notify_all()

    async def invalidate(self, media_uuids: tuple[str, ...]) -> None:
        """服务端缓存 miss 时清除本地就绪状态，下一请求重新携带数据。"""
        if not media_uuids:
            return
        async with self._condition:
            for media_uuid in media_uuids:
                self._states.pop(media_uuid, None)
                self._ready_order.pop(media_uuid, None)
            self._condition.notify_all()

    def _trim_ready_media(self) -> None:
        """限制客户端影子状态；淘汰后由下一请求安全重填。"""
        while len(self._ready_order) > self._maximum_ready_media:
            media_uuid, _ = self._ready_order.popitem(last=False)
            if self._states.get(media_uuid) == "ready":
                self._states.pop(media_uuid, None)


def _collect_media(
    messages: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """按首次出现顺序收集带完整数据和 UUID 的图片内容块。"""
    media: dict[str, dict[str, Any]] = {}
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "image_url":
                continue
            media_uuid = block.get("uuid")
            if not isinstance(media_uuid, str) or not media_uuid.strip():
                continue
            image_url = block.get("image_url")
            if not isinstance(image_url, dict) or not isinstance(
                image_url.get("url"), str
            ):
                raise ValueError(
                    "进入 MLLMClient 的 UUID 图片必须保留完整 image_url，"
                    "引用替换只能由统一协调器执行"
                )
            previous = media.setdefault(media_uuid, image_url)
            if previous != image_url:
                raise ValueError("同一媒体 UUID 在单次请求中对应了不同图片")
    return media


def _replace_media_with_references(
    messages: list[dict[str, Any]],
    referenced_media_uuids: set[str],
) -> list[dict[str, Any]]:
    """仅复制消息容器，避免复制未替换的 Base64 字符串。"""
    if not referenced_media_uuids:
        return messages
    replaced_messages: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            replaced_messages.append(message)
            continue
        replaced_content: list[Any] = []
        changed = False
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "image_url"
                and block.get("uuid") in referenced_media_uuids
            ):
                replaced_content.append({**block, "image_url": None})
                changed = True
            else:
                replaced_content.append(block)
        replaced_messages.append(
            {**message, "content": replaced_content} if changed else message
        )
    return replaced_messages


def strip_media_reference_metadata(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """关闭引用能力时移除 vLLM 专属 UUID，恢复标准 OpenAI 图片块。"""
    stripped_messages: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            stripped_messages.append(message)
            continue
        stripped_content: list[Any] = []
        changed = False
        for block in content:
            if isinstance(block, dict) and "uuid" in block:
                stripped_content.append(
                    {name: value for name, value in block.items() if name != "uuid"}
                )
                changed = True
            else:
                stripped_content.append(block)
        stripped_messages.append(
            {**message, "content": stripped_content} if changed else message
        )
    return stripped_messages


_LOOP_COORDINATORS: WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[tuple[str, str], VLLMMediaReferenceCoordinator],
] = WeakKeyDictionary()


def get_vllm_media_reference_coordinator(
    *,
    base_url: str,
    model: str,
) -> VLLMMediaReferenceCoordinator:
    """返回当前事件循环、服务地址和模型独占的协调器。"""
    loop = asyncio.get_running_loop()
    coordinators = _LOOP_COORDINATORS.setdefault(loop, {})
    key = (base_url.rstrip("/"), model)
    coordinator = coordinators.get(key)
    if coordinator is None:
        coordinator = VLLMMediaReferenceCoordinator()
        coordinators[key] = coordinator
    return coordinator


def is_vllm_media_cache_miss(error: BaseException) -> bool:
    """识别 vLLM 引用在服务重启或缓存淘汰后的稳定错误语义。"""
    response = getattr(error, "response", None)
    text = getattr(response, "text", "")
    return (
        getattr(error, "status_code", None) == 400
        and isinstance(text, str)
        and "cache miss for" in text.lower()
        and "data is not provided" in text.lower()
    )


__all__ = [
    "VLLMMediaReferenceCoordinator",
    "VLLMMediaReferenceRequest",
    "get_vllm_media_reference_coordinator",
    "is_vllm_media_cache_miss",
    "strip_media_reference_metadata",
]
