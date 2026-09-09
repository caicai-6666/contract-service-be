"""模型消息中的进程内 PNG 引用；只有传输边界才生成 Base64。"""

from __future__ import annotations

from base64 import b64encode
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PNGImage(BaseModel):
    """共享不可变 PNG 字节，序列化审计时只暴露内容指纹。"""

    model_config = ConfigDict(frozen=True)

    png_bytes: bytes = Field(repr=False, exclude=True)
    content_sha256: str

    def __deepcopy__(self, memo: dict[int, Any] | None = None) -> PNGImage:
        # 并行分支可以复制消息容器，但不需要复制不可变的图像引用。
        return self


def image_reference_json(value: Any) -> dict[str, str]:
    """公共前缀指纹只计算图片身份，避免为了哈希临时编码整份合同。"""
    if isinstance(value, PNGImage):
        return {"png_sha256": value.content_sha256}
    raise TypeError(f"不能序列化的上下文类型：{type(value).__name__}")


def materialize_image_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """生成本次请求的 JSON 消息，不改写公共前缀、不缓存编码结果。

    必须在 UUID 缓存引用替换之后调用，已经命中的页面不会触发编码。
    同一请求重复引用同一个 PNG 时仅编码一次。
    """
    encoded: dict[int, str] = {}
    result: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            result.append(message)
            continue
        blocks: list[Any] = []
        for block in content:
            image_url = block.get("image_url") if isinstance(block, dict) else None
            reference = image_url.get("url") if isinstance(image_url, dict) else None
            if isinstance(reference, PNGImage):
                key = id(reference.png_bytes)
                if key not in encoded:
                    encoded[key] = "data:image/png;base64," + b64encode(
                        reference.png_bytes
                    ).decode("ascii")
                blocks.append({**block, "image_url": {**image_url, "url": encoded[key]}})
            else:
                blocks.append(block)
        result.append({**message, "content": blocks})
    return result
