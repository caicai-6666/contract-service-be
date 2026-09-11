"""业务门禁输入、文件与文字三态、联合维度布尔结果及程序聚合结果。"""

from typing import Literal
from typing_extensions import TypedDict
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agent.contract_communication.business_gate.subgraph.file_readability.state import FileReadabilityState, ReadabilityFile
from .schema import FileSummaryGeneration, TextBusinessRelevanceGeneration, TextBusinessRelevanceDecision
from .schema import FileBusinessRelevanceDecision, NonBlankText
from .schema import ContextRelevanceBasis, ContextRelevanceGeneration
from app.schema.communication import ConversationHistoryRecord


class BusinessGateInput(TypedDict, total=False):
    """只接受原始输入；不接受调用方预先指定检查结果或渲染对象。"""

    files: tuple[ReadabilityFile, ...]
    text: str | None
    # 服务端选取最近至多五轮；不是前端可提交的历史覆盖字段。
    context: tuple[ConversationHistoryRecord, ...]


# 文件和文字使用三态，联合维度仍用 bool；执行状态不混同于业务判断。
RelevanceStatus = bool | Literal['skipped', 'not_implemented', 'failed']
TextRelevanceStatus = TextBusinessRelevanceDecision | Literal['skipped', 'not_implemented', 'failed']
FileRelevanceStatus = FileBusinessRelevanceDecision | Literal['skipped', 'not_implemented', 'failed']


class RelevanceFeedback(BaseModel):
    """单个相关性维度的业务日志，不代表整轮准入结论，也不包含模型私有推理。"""

    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    node: Literal['文件业务相关性', '文字业务相关性', '文件与文字相关性', '上下文相关性'] = Field(description='产生日志的相关性节点')
    result: bool | Literal['related', 'uncertain', 'unrelated', 'failed'] = Field(
        description='文件和文字业务维度使用三态，联合维度使用布尔值；failed 仅表示检查未完成')
    hint: NonBlankText = Field(description='程序根据最终检查结果生成的业务提示，不是面向用户的最终答复')

    @model_validator(mode='after')
    def validate_dimension(self):
        if self.result != 'failed':
            business_dimension = self.node in {'文件业务相关性', '文字业务相关性'}
            if business_dimension and type(self.result) is bool:
                raise ValueError('文件和文字业务相关性反馈必须使用三态结果')
            if not business_dimension and type(self.result) is not bool:
                raise ValueError('联合相关性反馈必须使用布尔结果')
        return self


class FileSummary(BaseModel):
    """每份文件的轻量描述；身份由程序绑定，名称不是磁盘路径或唯一标识。"""

    model_config = ConfigDict(extra='forbid', frozen=True, revalidate_instances='always')
    file_index: int = Field(ge=0, strict=True, description='本轮原始文件列表的下标，从 0 开始，由程序绑定')
    original_file_name: str = Field(min_length=1, description='原始上传文件名，由程序保留，不由模型改写')
    display_name: str = Field(min_length=1, description='根据文件内容生成的易于识别的名称，不改变文件存储路径')
    summary: str = Field(min_length=1, description='仅依据当前文件内容生成的轻量概述，供相关性判断和后续按需查阅使用')

    @field_validator('display_name', 'summary')
    @classmethod
    def reject_blank(cls, value):
        if not value.strip():
            raise ValueError('文件名称和摘要不能为空白')
        return value


class FileRelevanceInput(TypedDict, total=False):
    file_summaries: tuple[FileSummary, ...]


class FileSummaryIssue(BaseModel):
    """摘要未能生成的文件；只使用统一提示，不向前台模型暴露技术细节。"""

    model_config = ConfigDict(extra='forbid', frozen=True)
    file_index: int = Field(ge=1, strict=True, description='文件在本轮上传列表中的序号，从 1 开始，可区分同名文件')
    file_name: str = Field(min_length=1, description='未能生成摘要的文件原始上传名称，不作为路径使用')
    hint: Literal['文件摘要暂时未能生成，尚无法继续后续处理；这不代表文件内容存在问题。'] = Field(
        default='文件摘要暂时未能生成，尚无法继续后续处理；这不代表文件内容存在问题。',
        description='固定的摘要失败提示，不携带模型输出、异常详情或业务拒绝结论')


class FileSummaryFeedback(BaseModel):
    """前台模型可消费的摘要业务日志；无法可靠定位文件时不伪造问题条目。"""

    model_config = ConfigDict(extra='forbid', frozen=True)
    node: Literal['文件摘要生成'] = Field(default='文件摘要生成', description='产生日志的处理节点')
    hint: str = Field(min_length=1, description='实际处理范围；整批收束后说明未进入相关性判断及业务分析')
    issues: tuple[FileSummaryIssue, ...] = Field(description='未能生成摘要的文件，按上传序号严格升序排列；无法可靠定位时为空')

    @model_validator(mode='after')
    def validate_order(self):
        indices = [issue.file_index for issue in self.issues]
        if indices != sorted(set(indices)):
            raise ValueError('摘要问题必须按上传序号排列且不重复')
        return self


class FileSummaryResult(BaseModel):
    """单文件执行结果；原响应和 reasoning 留在私有审计，不进入轻量摘要。"""

    model_config = ConfigDict(extra='forbid', frozen=True)
    file_index: int
    file_name: str
    status: Literal['completed', 'failed']
    generation: FileSummaryGeneration | None = Field(default=None, exclude=True, repr=False)
    error: str | None = None
    feedback: FileSummaryFeedback | None = Field(default=None, description='单文件摘要失败的统一反馈；成功时为空')
    audit: tuple[dict, ...] = Field(default=(), exclude=True, repr=False)

    @model_validator(mode='after')
    def validate_result(self):
        if self.status == 'completed':
            if self.generation is None or self.error is not None:
                raise ValueError('摘要完成必须具有已校验产物，不能携带错误')
        elif self.generation is not None or not self.error:
            raise ValueError('摘要失败必须说明原因，不能携带未接受的产物')
        if (self.status == 'failed') != (self.feedback is not None):
            raise ValueError('仅摘要失败结果必须携带统一反馈')
        if self.feedback is not None:
            issues = self.feedback.issues
            if (len(issues) != 1 or issues[0].file_index != self.file_index + 1
                or issues[0].file_name != self.file_name):
                raise ValueError('单文件摘要反馈必须对应当前文件及其上传序号')
        return self


class TextRelevanceInput(TypedDict, total=False):
    text: str | None


class FileBusinessRelevanceResult(BaseModel):
    """程序绑定的逐文件结果；保留轻量判断，理由和审计不进入普通序列化。"""

    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    file_index: int = Field(ge=0)
    file_name: str
    status: Literal['completed', 'failed']
    result: FileBusinessRelevanceDecision | None = None
    reasoning: NonBlankText | None = Field(default=None, exclude=True, repr=False)
    audit: tuple[dict, ...] = Field(default=(), exclude=True, repr=False)

    @model_validator(mode='after')
    def validate_result(self):
        if self.status == 'completed':
            if self.result is None or self.reasoning is None:
                raise ValueError('文件判断完成时必须保留结果与依据')
        elif self.result is not None or self.reasoning is not None:
            raise ValueError('失败文件不得携带未接受的业务判断')
        return self


class TextBusinessRelevanceResult(BaseModel):
    """文字判断私有执行记录；前端和其他相关性分支不消费理由或原响应。"""

    model_config = ConfigDict(extra='forbid', frozen=True)
    status: Literal['completed', 'failed']
    generation: TextBusinessRelevanceGeneration | None = Field(default=None, exclude=True, repr=False)
    audit: tuple[dict, ...] = Field(default=(), exclude=True, repr=False)

    @model_validator(mode='after')
    def validate_result(self):
        if (self.status == 'completed') != (self.generation is not None):
            raise ValueError('文字判断仅在完成时持有已校验结果')
        return self


class FileTextRelevanceInput(FileRelevanceInput, TextRelevanceInput, total=False):
    pass


class FileTextRelevanceResult(BaseModel):
    """整轮文件文字关联的单个私有结果；失败不是 false。"""

    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    status: Literal['completed', 'failed']
    result: bool | None = None
    reasoning: NonBlankText | None = Field(default=None, exclude=True, repr=False)
    audit: tuple[dict, ...] = Field(default=(), exclude=True, repr=False)

    @model_validator(mode='after')
    def validate_result(self):
        if self.status == 'completed':
            if self.result is None or self.reasoning is None:
                raise ValueError('整体判断完成时必须保留布尔结果与依据')
        elif self.result is not None or self.reasoning is not None:
            raise ValueError('失败判断不得携带未接受的结果')
        return self


class ContextRelevanceInput(FileTextRelevanceInput, total=False):
    context: tuple[ConversationHistoryRecord, ...]


class ContextRelevanceResult(BaseModel):
    """完整判断与审计只供内部检查，不混入用户轨迹或其他分支。"""

    model_config = ConfigDict(extra='forbid', frozen=True)
    status: Literal['completed', 'failed']
    generation: ContextRelevanceGeneration | None = Field(default=None, exclude=True, repr=False)
    audit: tuple[dict, ...] = Field(default=(), exclude=True, repr=False)

    @model_validator(mode='after')
    def validate_result(self):
        if (self.status == 'completed') != (self.generation is not None):
            raise ValueError('上下文判断仅在完成时持有已校验结果')
        return self


class RejectionReply(BaseModel):
    """拒绝出口的权威回复；保留原始检查状态，技术失败不混同于材料不相关。"""

    model_config = ConfigDict(extra='forbid', frozen=True)
    source_status: Literal['rejected', 'failed', 'not_implemented']
    message: NonBlankText
    fallback: bool
    audit: tuple[dict, ...] = Field(default=(), exclude=True, repr=False)


class BusinessGateSubgraphState(FileReadabilityState, total=False):
    """skipped 表示未调度，not_implemented 表示已进入占位节点，不代表判断通过。"""

    text: str | None
    context: tuple[ConversationHistoryRecord, ...]
    file_business_relevance: FileRelevanceStatus
    file_business_relevance_results: tuple[FileBusinessRelevanceResult, ...]
    file_business_relevance_feedback: RelevanceFeedback | None
    text_business_relevance: TextRelevanceStatus
    text_business_relevance_result: TextBusinessRelevanceResult | None
    text_business_relevance_feedback: RelevanceFeedback | None
    file_text_relevance: RelevanceStatus
    file_text_relevance_result: FileTextRelevanceResult | None
    file_text_relevance_feedback: RelevanceFeedback | None
    context_relevance: RelevanceStatus
    context_relevance_basis: ContextRelevanceBasis | None
    context_relevance_result: ContextRelevanceResult | None
    context_relevance_feedback: RelevanceFeedback | None
    status: Literal['not_implemented', 'passed', 'rejected', 'failed']
    relevance_score: int | None
    relevance_threshold: int | None
    file_summaries: tuple[FileSummary, ...]
    file_summary_status: Literal['skipped', 'not_implemented', 'completed', 'failed']
    file_summary_results: tuple[FileSummaryResult, ...]
    file_summary_feedback: FileSummaryFeedback | None
    rejection_reply: RejectionReply



__all__ = ["BusinessGateInput", "BusinessGateSubgraphState", "RelevanceStatus", "FileSummary", "FileSummaryResult",
           "FileSummaryIssue", "FileSummaryFeedback", "RelevanceFeedback", "RejectionReply",
           "FileRelevanceInput", "TextRelevanceInput", "FileTextRelevanceInput", "ContextRelevanceInput"]
