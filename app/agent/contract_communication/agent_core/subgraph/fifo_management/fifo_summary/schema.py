"""工作记忆主题与两区域输出契约；旧五区域仅作为输入兼容。"""
from typing import Literal
from pydantic import Field, model_validator
from app.schema.communication_workspace import WorkspaceObject, WorkspaceText
from ..compression import FIFOCompressionRequest
from ..schema import FIFOTask, FIFORange


class SummaryTopic(WorkspaceObject):
    topic_id: WorkspaceText = Field(description='本次主题的唯一标识，供并发结果对齐，不是任务编号。')
    title: WorkspaceText = Field(description='对后续继续任务有用的记忆主题名称，可跨多个任务，不以任务数量拆分。')
    scope: WorkspaceText = Field(description='一至三句话的简要提取指导，说明有继续使用价值的当前状态、关键限制、未决事项及必要排除范围；不写完整摘要、具体事实清单、状态结论或新待办。来源由task_ids与previous_topic_ids指定。')
    previous_topic_ids: list[WorkspaceText] = Field(description='旧摘要主题标识，可合并或拆分；无旧主题来源时显式传空列表，不能引用本次新主题标识。')
    task_ids: list[WorkspaceText] = Field(description='本次压缩轨迹中与主题相关的任务标识，可以为空表示只整理旧摘要。')


class TopicPlan(WorkspaceObject):
    topics: list[SummaryTopic] = Field(max_length=32, description='按输出顺序排列的记忆主题，最多32个；允许舍弃低价值历史，不遗漏会影响后续工作的有效信息。')


class SummaryItem(WorkspaceObject):
    content: WorkspaceText = Field(description='精简业务内容，保留条件、冲突、实际状态和必要限制，不记录系统提示。')
    task_ids: list[WorkspaceText] = Field(description='本次相关任务出处；来自旧摘要的条目可为空。')
    sources: list[WorkspaceText] = Field(description='原材料来源或旧摘要中已有的来源说明，不把本摘要自身当作独立证据。')


class LegacyTopicSummaryContent(WorkspaceObject):
    """仅用于读取旧五区域摘要；保持原区域含义，不直接扁平合并为当前事实。"""
    user_requirements: list[SummaryItem] = Field(description='当前仍有效的用户目标、约束、偏好和暂停指令；不保留已完成的历史提问，不从资料推导要求。无独立内容时空列表。')
    key_information: list[SummaryItem] = Field(description='截至轨迹结束时可继续使用的事实、条款、条件、确认状态和必要的不确定性；不复述核验过程，不把失效结论作为当前事实。无独立内容时空列表。')
    actions_and_results: list[SummaryItem] = Field(description='对后续有用、可避免重复探索的尝试、方法及成功、失败或受阻的实际结果和限制；仅依据已提供的业务记录，不逐轮复述工具日志，不重复普通查询确认的事实，不把调用发出当成成功。无独立内容时空列表。')
    delivered_content: list[SummaryItem] = Field(description='已经向用户交付的结论或文件等内容的标识、已有版本和覆盖范围；不重复交付物中的事实全文，内部生成不等于已交付。无独立内容时空列表。')
    open_items: list[SummaryItem] = Field(description='截至轨迹结束时仍未解决的问题、缺少的必要前提及未完成事项；移除已解决或撤销的缺口，暂停事项保留暂停限制，不新增调查计划。无独立内容时空列表。')


class LegacyTopicSummary(LegacyTopicSummaryContent):
    topic_id: WorkspaceText = Field(description='旧摘要主题标识。')
    title: WorkspaceText = Field(description='旧摘要主题名称。')


class TopicSummaryContent(WorkspaceObject):
    """用于继续任务的两区域记忆；模型仅生成业务内容。"""
    current_memory: list[SummaryItem] = Field(description='对后续继续任务有用的当前事实、必要条件、有效要求及重要交付状态；省略已失效状态和常规过程，必要历史仅保留一句，不把旧五区域内容全部搬入。无内容时空列表。')
    open_items: list[SummaryItem] = Field(description='仍未解决的问题、必要前提及暂停状态；不保留已解决或撤销事项，不把范围限制自动扩展为新调查，不新增计划。无内容时空列表。')


class TopicSummary(TopicSummaryContent):
    topic_id: WorkspaceText = Field(description='程序填入的规划主题标识。')
    title: WorkspaceText = Field(description='程序填入的规划主题名称。')


class TopicGenerationOutput(WorkspaceObject):
    """模型只提交业务摘要，不生成主题身份或 reasoning。"""
    summary: TopicSummaryContent = Field(description='依据分配资料形成的两区域记忆内容，不包含 reasoning、topic_id 或 title。')


class TopicGenerationRequest(WorkspaceObject):
    topic: SummaryTopic = Field(description='本分支唯一主题及边界。')
    tasks: list[FIFOTask] = Field(description='仅包含主题引用的本次有效业务任务副本。')
    previous_summary: dict | None = Field(description='只包含 previous_topic_ids 对应的旧主题，供本分支合并；无引用时为 null。')


class TopicMapResult(WorkspaceObject):
    topic_id: WorkspaceText = Field(description='分支对应的规划主题标识，失败时同样保留。')
    status: Literal['success', 'error'] = Field(description='本主题生成及结构验收的结果。')
    summary: TopicSummary | None = Field(default=None, description='成功时的完整单主题摘要；失败时为空。')
    error_type: Literal['not_implemented', 'generation_error'] | None = Field(default=None, description='失败类别，不携带原始异常或模型输出。')

    @model_validator(mode='after')
    def validate_outcome(self):
        if self.status == 'success':
            if self.summary is None or self.error_type is not None or self.summary.topic_id != self.topic_id:
                raise ValueError('成功分支必须包含对应主题摘要且不能含错误')
        elif self.summary is not None or self.error_type is None:
            raise ValueError('失败分支只能包含错误类别')
        return self


class FIFOTopicSummary(WorkspaceObject):
    version: Literal['fifo-topic-summary-v2'] = Field(default='fifo-topic-summary-v2', description='程序维护的摘要结构版本。')
    latest_coverage: FIFORange = Field(description='程序确定的本次压缩范围，不表示完整历史覆盖或语义无损。')
    topics: list[TopicSummary] = Field(description='按规划顺序排列的全部成功主题；允许为空，不隐式继承旧主题。')


class FIFOSummaryResult(WorkspaceObject):
    status: Literal['success', 'error'] = Field(description='所有主题验收后才成功，失败不返回部分摘要。')
    summary: dict | None = Field(default=None, description='成功时包含主题列表及程序填写的本次覆盖范围。')
    error_type: Literal['input_error', 'not_implemented', 'generation_error'] | None = Field(default=None, description='失败类别，区分模型核心占位和实际调用失败。')
    error_feedback: str | None = Field(default=None, description='最小错误反馈，不包含原始模型输出或内部异常。')


def topic_generation_json_schema() -> dict:
    r"""解码器避免使用搜索语义的单字符正则，本地仍执行非空白校验。

    WorkspaceText 的 \S 在本地表示包含非空白字符；部分约束解码器
    按完整匹配处理，会把所有文本限制为单字符。解码侧仅约束非空，
    完整响应仍由 TopicGenerationOutput 校验，不能绕过空白文本检查。
    """
    schema = TopicGenerationOutput.model_json_schema()

    def visit(value):
        if isinstance(value, dict):
            if value.get('pattern') == r'\S':
                del value['pattern']
                value['minLength'] = 1
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)
    return schema
