"""HTML、提取文本和精提候选只在子图内部流转，不进入外层工具结果。"""
from typing_extensions import TypedDict
from .schema import WebPageRequest, WebPageResult
from .image import WebImage


class WebPageInput(TypedDict):
    request: WebPageRequest | dict


class WebPageOutput(TypedDict):
    result: WebPageResult


class WebPageState(WebPageInput, total=False):
    html: bytes
    extracted_text: str
    image_candidates: list[dict]
    images: tuple[WebImage, ...]
    selected_images: tuple[WebImage, ...]
    image_hint: str
    access_hint: str
    has_content: bool
    refined_content: str
    hint: str | None
    final_url: str | None
    fetched_at: str | None
    error_code: str | None
    failed_stage: str | None
    result: WebPageResult
