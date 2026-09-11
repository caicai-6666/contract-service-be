"""文件可读性输入、检查结果、公共图状态及私有 Map 分支状态。"""

from __future__ import annotations

import operator
from typing import Annotated, Literal

from typing_extensions import TypedDict
from pydantic import BaseModel, ConfigDict, Field, StrictBytes, model_validator

from app.tool.pdf_open import PDFOpenErrorCode
from app.agent.contract_extraction.state import PreparedPDFPage
from .schema import VisualReadabilityJudgment


class ReadabilityModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class ReadabilityFile(ReadabilityModel):
    """保持原始上传顺序的私有输入，不依赖 service 的运行时类型。"""

    file_name: str = Field(min_length=1, description='用户上传的原始文件名，仅用于展示，不作为磁盘路径')
    content: StrictBytes = Field(repr=False, description='内存中的原始上传字节，空内容由打开检查明确拒绝')


class OpenedFile(ReadabilityModel):
    file_index: int = Field(ge=0, description='文件在本轮原始上传列表中的下标，从 0 开始')
    file_name: str = Field(min_length=1, description='原始文件名，同名文件通过下标区分')
    page_count: int = Field(gt=0, description='打开检查读取到的总页数，不代表这些页面已成功渲染')


class FileOpenFailure(ReadabilityModel):
    file_index: int = Field(ge=0, description='首个打开失败文件在原始列表中的下标，从 0 开始')
    file_name: str = Field(min_length=1, description='打开失败文件的原始文件名')
    code: PDFOpenErrorCode = Field(description='确定性的打开失败类别')
    reason: str = Field(min_length=1, description='可向用户展示的拒绝原因，不含底层异常堆栈')


class FileOpenIssue(ReadabilityModel):
    """供前台模型定位问题文件；此处使用自然语言中的上传序号。"""

    file_index: int = Field(ge=1, strict=True, description='本轮上传序号，从 1 开始；由内部零基下标加 1 得到，可区分同名文件')
    file_name: str = Field(min_length=1, description='出问题文件的原始上传名称，不作路径使用')
    hint: str = Field(min_length=1, description='程序根据实际打开失败类别生成的原因，不包含异常堆栈或未经确认的结论')


class FileOpenFeedback(ReadabilityModel):
    """节点业务日志，不是面向用户的最终答复；首错即停，因此只有一个问题。"""

    node: Literal['文件打开检查'] = Field(default='文件打开检查', description='产生日志的处理节点')
    hint: str = Field(min_length=1, description='本轮文件数量、停止位置、已经检查和未检查的范围及后续处理情况')
    issues: tuple[FileOpenIssue, ...] = Field(min_length=1, max_length=1, description='本节点发现的首个失败文件；不意味着只有该文件有问题')


class FileOpenCheck(ReadabilityModel):
    status: Literal['passed', 'rejected', 'skipped']
    opened_files: tuple[OpenedFile, ...] = ()
    failure: FileOpenFailure | None = None
    feedback: FileOpenFeedback | None = Field(default=None, description='仅拒绝时保存，供后续前台模型生成反馈；通过或跳过时为空')

    @model_validator(mode='after')
    def validate_result(self):
        if (self.status == 'rejected') != (self.failure is not None):
            raise ValueError('只有拒绝结果必须包含失败信息')
        if (self.status == 'rejected') != (self.feedback is not None):
            raise ValueError('只有拒绝结果必须包含打开检查反馈日志')
        if self.status == 'passed' and not self.opened_files:
            raise ValueError('打开检查通过必须至少包含一份文件')
        if self.status == 'skipped' and self.opened_files:
            raise ValueError('无文件跳过不能包含文件结果')
        if [f.file_index for f in self.opened_files] != list(range(len(self.opened_files))):
            raise ValueError('已打开文件必须保持连续的原始上传顺序')
        if self.failure is not None and self.failure.file_index != len(self.opened_files):
            raise ValueError('只能报告按顺序遇到的首个失败文件')
        if self.feedback is not None:
            issue = self.feedback.issues[0]
            if (issue.file_index != self.failure.file_index + 1
                or issue.file_name != self.failure.file_name):
                raise ValueError('反馈日志必须对应实际失败文件及其上传序号')
        return self


class RenderedFile(ReadabilityModel):
    """只驻留逐页 PNG 与模型页面元数据，不组装或缓存完整 PDF。"""

    file_index: int = Field(ge=0)
    file_name: str = Field(min_length=1)
    page_count: int = Field(gt=0)
    total_visual_tokens: int = Field(gt=0)
    visual_tokens_per_page_budget: int = Field(gt=0)
    visual_tokens_per_request_budget: int = Field(gt=0)
    pages: tuple[PreparedPDFPage, ...]

    @model_validator(mode='after')
    def validate_pages(self):
        if [p.page_number for p in self.pages] != list(range(1, self.page_count + 1)):
            raise ValueError('渲染页码必须从 1 开始连续覆盖整份文件')
        if (self.total_visual_tokens != sum(p.visual_tokens for p in self.pages)
            or self.total_visual_tokens > self.visual_tokens_per_request_budget):
            raise ValueError('渲染文件视觉总预算不一致或超限')
        if any(not p.png_bytes or p.width_pixels <= 0 or p.height_pixels <= 0
               or not 0 < p.visual_tokens <= self.visual_tokens_per_page_budget for p in self.pages):
            raise ValueError('渲染页面为空、尺寸无效或超出单页预算')
        return self


class FileRenderFailure(ReadabilityModel):
    file_index: int = Field(ge=0)
    file_name: str = Field(min_length=1)
    page_number: int | None = Field(default=None, ge=1)
    code: Literal['render_failed', 'visual_budget_exceeded', 'resource_unavailable']
    reason: str = Field(min_length=1)


class FileRenderIssue(ReadabilityModel):
    """页面渲染问题；上传序号从1开始，原始名称不作为文件路径。"""

    file_index: int = Field(ge=1, strict=True, description='问题文件在本轮上传列表中的序号，从 1 开始，用于区分同名文件')
    file_name: str = Field(min_length=1, description='问题文件的原始上传名称')
    hint: str = Field(min_length=1, description='程序依据实际渲染失败生成的原因，只在确知时注明页码，不包含异常堆栈')


class FileRenderFeedback(ReadabilityModel):
    """供前台模型使用的业务日志；汇总后保留所有渲染问题，不只保留代表性失败。"""

    node: Literal['页面渲染'] = Field(default='页面渲染', description='产生日志的处理节点')
    hint: str = Field(min_length=1, description='实际处理范围；仅问题文件未进入视觉判断，不能推断其他文件的进度')
    issues: tuple[FileRenderIssue, ...] = Field(min_length=1, description='按上传顺序排列的渲染失败文件；单文件分支只有一项')

    @model_validator(mode='after')
    def validate_order(self):
        indices = [issue.file_index for issue in self.issues]
        if indices != sorted(set(indices)):
            raise ValueError('渲染问题必须按上传序号排列且不重复')
        return self


class FileRenderCheck(ReadabilityModel):
    status: Literal['not_started', 'skipped', 'passed', 'rejected', 'failed']
    failure: FileRenderFailure | None = None
    feedback: FileRenderFeedback | None = Field(default=None, description='渲染拒绝或失败时的 hint 对象；通过、跳过或尚未开始时为空')

    @model_validator(mode='after')
    def validate_failure(self):
        if (self.status in {'rejected', 'failed'}) != (self.failure is not None):
            raise ValueError('渲染失败必须有原因，非失败结果不能携带错误')
        if (self.status in {'rejected', 'failed'}) != (self.feedback is not None):
            raise ValueError('渲染拒绝或失败必须包含反馈日志，其他状态不能携带')
        if self.feedback is not None and not any(
            issue.file_index == self.failure.file_index + 1 and issue.file_name == self.failure.file_name
            for issue in self.feedback.issues
        ):
            raise ValueError('渲染反馈必须包含实际失败文件及其上传序号')
        return self


class FileReadabilityState(TypedDict, total=False):
    """打开通过不代表渲染或业务通过；整体仍不得输出放行结论。"""

    files: tuple[ReadabilityFile, ...]
    status: Literal['not_implemented', 'rejected', 'failed']
    open_check: FileOpenCheck
    render_check: FileRenderCheck
    rendered_files: tuple[RenderedFile, ...]
    visual_check: FileVisualCheck
    visual_results: tuple[FileVisualResult, ...]
    user_hints: tuple[str, ...]


class FileVisualIssue(ReadabilityModel):
    """视觉检查问题；仅保存已校验的模型提示或程序生成的失败说明。"""

    file_index: int = Field(ge=1, strict=True, description='问题文件在本轮上传列表中的序号，从 1 开始，可区分同名文件')
    file_name: str = Field(min_length=1, description='问题文件的原始上传名称，不作为路径使用')
    hint: str = Field(min_length=1, description='不可读时采用已校验的模型 hint；执行失败时采用程序说明，不包含推理、无效输出或纠错过程')


class FileVisualFeedback(ReadabilityModel):
    """前台反馈所需的视觉检查日志，不代替判定结果或最终用户消息。"""

    node: Literal['视觉可读性检查'] = Field(default='视觉可读性检查', description='产生日志的处理节点')
    hint: str = Field(min_length=1, description='实际处理范围；只在汇总结束后说明整轮停止，不推断其他文件正常')
    issues: tuple[FileVisualIssue, ...] = Field(min_length=1, description='按上传顺序排列的视觉检查问题，不包含渲染失败而未检查的文件')

    @model_validator(mode='after')
    def validate_order(self):
        indices = [issue.file_index for issue in self.issues]
        if indices != sorted(set(indices)):
            raise ValueError('视觉检查问题必须按上传序号排列且不重复')
        return self


class FileVisualResult(ReadabilityModel):
    """只接收校验成功的判断；审计留在私有状态，不进入普通序列化。"""

    file_index: int = Field(ge=0)
    file_name: str
    status: Literal['passed', 'rejected', 'failed', 'skipped']
    judgment: VisualReadabilityJudgment | None = None
    error: str | None = None
    feedback: FileVisualFeedback | None = Field(default=None, description='单文件不可读或执行失败时的反馈日志；可读或未检查时为空')
    audit: tuple[dict, ...] = Field(default=(), exclude=True, repr=False)

    @model_validator(mode='after')
    def validate_outcome(self):
        if self.status in {'passed', 'rejected'}:
            if self.judgment is None or self.error is not None:
                raise ValueError('业务结论必须有已校验判断且不能有技术错误')
            if self.judgment.result != (self.status == 'passed'):
                raise ValueError('文件状态必须与布尔判断一致')
        elif self.judgment is not None:
            raise ValueError('未完成视觉校验不得携带判断')
        if (self.status == 'failed') != (self.error is not None):
            raise ValueError('只有执行失败必须携带错误原因')
        if (self.status in {'rejected', 'failed'}) != (self.feedback is not None):
            raise ValueError('只有视觉拒绝或失败必须包含反馈日志')
        if self.feedback is not None:
            issue = self.feedback.issues[0]
            if (len(self.feedback.issues) != 1 or issue.file_index != self.file_index + 1
                or issue.file_name != self.file_name):
                raise ValueError('单文件视觉反馈必须对应当前文件及其上传序号')
            if self.status == 'rejected' and issue.hint != self.judgment.hint:
                raise ValueError('不可读反馈必须保留已校验的模型 hint，不得改写或使用其他输出')
        return self


class FileVisualCheck(ReadabilityModel):
    status: Literal['not_started', 'skipped', 'passed', 'rejected', 'failed']
    feedback: FileVisualFeedback | None = Field(default=None, description='汇总后的视觉问题日志；没有视觉拒绝或失败时为空')

    @model_validator(mode='after')
    def validate_feedback(self):
        if (self.status in {'rejected', 'failed'}) != (self.feedback is not None):
            raise ValueError('视觉检查汇总只有拒绝或失败时必须包含反馈日志')
        return self


class ReadabilityInput(TypedDict, total=False):
    files: tuple[ReadabilityFile, ...]


class ReadabilityGraphState(FileReadabilityState, total=False):
    # 仅 Map 分支追加，入口与汇总覆盖清空，不接受调用方伪造结果。
    _file_results: Annotated[tuple[dict, ...], operator.add]


class FileBranchState(TypedDict, total=False):
    file: ReadabilityFile
    metadata: OpenedFile
    rendered: RenderedFile | None
    render_check: FileRenderCheck
    visual: FileVisualResult
    _file_results: tuple[dict, ...]


class FileBranchOutput(TypedDict):
    _file_results: tuple[dict, ...]


__all__ = ['ReadabilityFile', 'OpenedFile', 'FileOpenFailure', 'FileOpenCheck', 'FileReadabilityState',
           'FileVisualIssue', 'FileVisualFeedback',
           'FileRenderIssue', 'FileRenderFeedback',
           'FileOpenIssue', 'FileOpenFeedback',
           'RenderedFile', 'FileRenderFailure', 'FileRenderCheck', 'FileVisualCheck', 'FileVisualResult',
           'ReadabilityInput', 'ReadabilityGraphState', 'FileBranchState', 'FileBranchOutput']
