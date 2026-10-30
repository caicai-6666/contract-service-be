"""首次读取网页的输入输出契约；来源和缓存由外层管理。"""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from .image import WebImage


class WebPageRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=True)
    url: HttpUrl = Field(description='外层根据来源标识解析出的 HTTP 或 HTTPS 地址。')
    focus: str = Field(min_length=1, description='感兴趣的重点，以自然语言说明希望从页面提取哪些信息；供后续模型精炼使用。')

    @model_validator(mode='after')
    def no_credentials(self):
        if self.url.username or self.url.password:
            raise ValueError('公开网页地址不能包含登录凭据')
        return self


class WebPageResult(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=True)
    status: Literal['success', 'no_content', 'error'] = Field(description='success为有效正文，no_content为正常读取后没有可用正文，error为执行故障。')
    url: str = Field(description='原始请求地址。')
    final_url: str | None = Field(default=None, description='HTTP 重定向后的实际地址，未取得时为空。')
    fetched_at: str | None = Field(default=None, description='抓取时间，带时区的 ISO 8601 字符串，未抓取时为空。')
    content: str | None = Field(default=None, description='仅success时为精提后的正文；无正文及执行失败时为空。')
    images: tuple[WebImage, ...] = Field(default=(), exclude=True, repr=False, description='实际选中的网页图片，仅驻留当前任务，不进入回执和持久化。')
    hint: str | None = Field(default=None, description='no_content时必填具体友好的无正文原因；也用于执行失败或正文不完整等提示。')
    error_code: str | None = Field(default=None, description='仅执行故障时提供机器原因；success/no_content时为空。')
    failed_stage: Literal['fetch_html', 'extract_text', 'refine_content', 'finalize'] | None = Field(
        default=None, description='仅执行故障时提供阶段；success/no_content时为空。')

    @model_validator(mode='after')
    def validate_outcome(self):
        if self.status == 'success':
            if not self.content or self.error_code is not None or self.failed_stage is not None:
                raise ValueError('成功结果必须包含正文且不得包含错误标记')
        elif self.status == 'no_content':
            if self.content is not None or self.images or not self.hint or self.error_code is not None or self.failed_stage is not None:
                raise ValueError('正常无正文必须说明原因，不携带正文、图片或错误标记')
        elif self.content is not None or self.images or not self.error_code or not self.hint or not self.failed_stage:
            raise ValueError('失败结果必须包含原因和阶段，且不能返回正文')
        return self


class WebPageRefinement(BaseModel):
    """模型普通 JSON 输出，Schema 只用于提示与本地校验。"""
    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=True)
    evidence: list[str] = Field(description='支持判断的内容依据，可摘录或概括；保持原文事实关系，不要求逐字引用；无相关依据时可为空列表。')
    reasoning: str = Field(min_length=1, description='简要说明目标对象、相关性、必要上下文及排除理由；区分噪声、不相关正文和归属不明，不补充外部事实。')
    type: Literal['content', 'no_content'] = Field(description='content 表示有可归属且与重点相关的有效信息；no_content 表示无可用信息，需要结束本次读取。')
    result: str = Field(min_length=1, description='content 时为围绕关注重点组织的精炼文本，可分段，保留条件、例外和限制；no_content 时为具体友好的无有效信息提示。')

    selected_images: list[Annotated[int, Field(strict=True, ge=1, description='本次实际提供的图片整数编号，从1开始。')]] = Field(default_factory=list,
        description='保留给主模型查看的图片编号，必须来自本次实际提供的图像，不可重复或超出允许数量；无相关图或no_content时为空列表。')
