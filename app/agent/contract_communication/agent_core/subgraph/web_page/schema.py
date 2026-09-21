"""首次读取网页的输入输出契约；来源和缓存由外层管理。"""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class WebPageRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=True)
    url: HttpUrl = Field(description='外层根据来源标识解析出的 HTTP 或 HTTPS 地址。')
    focus: str = Field(min_length=1, description='感兴趣的重点，以自然语言说明希望从页面提取哪些信息；供后续模型精炼使用。')


class WebPageResult(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=True)
    status: Literal['success', 'error'] = Field(description='仅有通过校验的有效正文才返回 success。')
    url: str = Field(description='原始请求地址。')
    final_url: str | None = Field(default=None, description='HTTP 重定向后的实际地址，未取得时为空。')
    fetched_at: str | None = Field(default=None, description='抓取时间，带时区的 ISO 8601 字符串，未抓取时为空。')
    content: str | None = Field(default=None, description='成功时为精提后的正文；失败时不得携带半成品。')
    hint: str | None = Field(default=None, description='失败原因，或成功时正文不完整、访问限制等提示。')
    error_code: str | None = Field(default=None, description='失败的机器可读原因；成功时为空。')
    failed_stage: Literal['fetch_html', 'extract_text', 'refine_content', 'finalize'] | None = Field(
        default=None, description='失败阶段；成功时为空。')

    @model_validator(mode='after')
    def validate_outcome(self):
        if self.status == 'success':
            if not self.content or self.error_code is not None or self.failed_stage is not None:
                raise ValueError('成功结果必须包含正文且不得包含错误标记')
        elif self.content is not None or not self.error_code or not self.hint or not self.failed_stage:
            raise ValueError('失败结果必须包含原因和阶段，且不能返回正文')
        return self


class WebPageRefinement(BaseModel):
    """模型普通 JSON 输出，Schema 只用于提示与本地校验。"""
    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=True)
    evidence: list[str] = Field(description='支持判断的内容依据，可摘录或概括；保持原文事实关系，不要求逐字引用；无相关依据时可为空列表。')
    reasoning: str = Field(min_length=1, description='简要说明目标对象、相关性、必要上下文及排除理由；区分噪声、不相关正文和归属不明，不补充外部事实。')
    type: Literal['content', 'no_content'] = Field(description='content 表示有可归属且与重点相关的有效信息；no_content 表示无可用信息，需要结束本次读取。')
    result: str = Field(min_length=1, description='content 时为围绕关注重点组织的精炼文本，可分段，保留条件、例外和限制；no_content 时为具体友好的无有效信息提示。')
