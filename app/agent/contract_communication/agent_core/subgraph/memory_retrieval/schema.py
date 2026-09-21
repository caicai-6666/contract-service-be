"""记忆检索子图契约；不作为主模型工具参数，也不负责分页资源。"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schema.communication import ConversationHistoryRecord


class RetrievalModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=True)


class MemoryRetrievalRequest(RetrievalModel):
    conversation_id: str = Field(min_length=1, description='由已鉴权的调用方注入的当前会话标识，不允许检索规划改写。')
    query: str = Field(min_length=1, description='自然语言检索需求，可包含明确的时间、任务终态及希望找回的内容。')
    reference_time: int = Field(ge=0, strict=True, description='当前任务创建时间，UTC Unix毫秒；用于解析昨天等相对时间，不使用节点执行时间替代。')
    kind: Literal['task'] = Field(default='task', description='固定只检索任务记录，排除summary记录。')


class FieldQuery(RetrievalModel):
    field: Literal['user_input', 'intermediate_output', 'final_output'] = Field(description='选中的三入口检索字段；该字段的BM25和向量召回同步启用。')
    query: str = Field(min_length=1, description='针对该字段改写的检索内容；文件可用file_name、display_name、summary中的任一或多个字段。')


class RetrievalPlan(RetrievalModel):
    queries: tuple[FieldQuery, ...] = Field(min_length=1, max_length=3, description='选中的字段及查询，每个字段最多出现一次。')
    created_from: int | None = Field(default=None, ge=0, description='任务创建时间下界，UTC Unix毫秒，包含边界；无明确要求时为空。')
    created_before: int | None = Field(default=None, ge=0, description='任务创建时间上界，UTC Unix毫秒，不含边界；无明确要求时为空。')
    statuses: tuple[Literal['completed', 'cancelled', 'superseded', 'rejected', 'failed', 'expired'], ...] | None = Field(default=None, description='明确要求的任务终态；为空表示不追加终态过滤，不等于所有状态都有检索投影。')

    @model_validator(mode='after')
    def validate_scope(self):
        if len({item.field for item in self.queries}) != len(self.queries):
            raise ValueError('检索字段不能重复')
        if self.created_from is not None and self.created_before is not None and self.created_from >= self.created_before:
            raise ValueError('时间下界必须小于上界')
        if self.statuses is not None and not self.statuses:
            raise ValueError('无状态过滤请使用null，不能传空集合')
        return self


class RankedTask(RetrievalModel):
    record: ConversationHistoryRecord = Field(description='从当前会话回读的原任务，包含创建时间、终态和完整轨迹，不是模型生成的任务摘要。')
    rrf_score: float = Field(ge=0, allow_inf_nan=False, description='按实际召回排名融合后的RRF分数，不是相似度或置信度。')

    @model_validator(mode='after')
    def validate_task(self):
        if self.record.kind != 'task':
            raise ValueError('检索结果不允许包含summary')
        return self


class MemoryRetrievalResult(RetrievalModel):
    status: Literal['success', 'error'] = Field(description='success包含真实空召回；error表示未实现、输入非法或执行失败。')
    query_id: str | None = Field(default=None, description='非空结果的驻留标识；空结果或纯召回阶段未分配时为空。')
    total: int = Field(default=0, ge=0, description='已建模查询保留的任务条数。')
    total_pages: int = Field(default=0, ge=0, description='已建模查询页数，空结果为0页，不创建驻留标识。')
    tasks: tuple[RankedTask, ...] = Field(default=(), description='成功时按RRF降序排列的任务集合，供外层建模分页；失败时为空。')
    error_code: str | None = Field(default=None, description='失败分类；成功时为空。')
    error: str | None = Field(default=None, description='失败说明，不包含私有推理或异常堆栈。')

    @model_validator(mode='after')
    def validate_result(self):
        if self.status == 'error' and (self.tasks or not self.error_code or not self.error):
            raise ValueError('失败必须提供错误信息且不发布部分任务')
        if self.status == 'success' and (self.error_code is not None or self.error is not None):
            raise ValueError('成功不能携带错误')
        return self
