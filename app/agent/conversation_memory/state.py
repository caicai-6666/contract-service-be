"""会话记忆批处理图的输入、计划及结果边界。"""

from typing import Annotated, Literal
import operator
import math
from typing_extensions import TypedDict
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from app.agent.conversation_memory.tool import MemorySelectionResult, SelectedMemoryTask


def _validate_embedding(vector):
    if len(vector) != 4096 or not all(math.isfinite(x) for x in vector) or not math.isclose(
        math.hypot(*vector), 1.0, rel_tol=1e-6, abs_tol=1e-6
    ):
        raise ValueError('记忆向量必须为4096维、有限且L2归一化')
    return vector


MemoryEmbedding = Annotated[tuple[float, ...], AfterValidator(_validate_embedding)]


class MemoryTaskInput(BaseModel):
    """一份冻结任务的公开轨迹；任务标识用于批内关联，不携带认证凭据。"""

    model_config = ConfigDict(extra='forbid', frozen=True)

    task_id: str = Field(min_length=1, description='待加工任务的稳定标识，由服务层映射原任务；批内必须唯一。')
    created_at: int | None = Field(
        default=None, strict=True, ge=0, le=253402271999999,
        description='原任务创建时间，UTC Unix毫秒，与历史记录created_at一致；缺失时为null，不使用整理时间补填。最大支持9999-12-30 UTC，以保证+08:00展示不溢出。',
    )
    status: Literal['completed', 'cancelled', 'superseded', 'rejected', 'failed', 'expired'] = Field(
        description='原任务终态，不能将中断或失效内容作为已确认结论。',
    )
    payload: dict = Field(description='冻结的公开 input/trace 内容，不得包含私有审计或认证凭据。')
    activated_at: int | None = Field(default=None, strict=True, ge=0, description='原任务激活时间，UTC Unix毫秒；只复制，不推断。')
    processing_duration_ms: int | None = Field(default=None, strict=True, ge=0, description='原任务总处理时长，毫秒；只复制，不以记忆加工耗时替代。')


class MemoryGenerationInput(BaseModel):
    """同一授权处理范围内的一批任务，顺序由调用方保留。"""

    model_config = ConfigDict(extra='forbid', frozen=True)

    tasks: tuple[MemoryTaskInput, ...] = Field(min_length=1, description='非空待处理任务列表；不得混入未授权用户的任务。')

    @model_validator(mode='after')
    def require_unique_tasks(self):
        if len({task.task_id for task in self.tasks}) != len(self.tasks):
            raise ValueError('批内任务标识不能重复')
        return self


class MemoryTaskResult(BaseModel):
    """单任务终态；审计仅供内部追溯，不拼入检索正文或前端历史。"""

    model_config = ConfigDict(extra='forbid', frozen=True)
    task_id: str = Field(min_length=1)
    task_number: int = Field(ge=1)
    status: Literal['summarized', 'no_memory', 'failed']
    retrieval_text: str | None = Field(default=None, min_length=1, max_length=6000)
    embedding: MemoryEmbedding | None = None
    evidence: str | None = None
    reasoning_summary: str | None = None
    error: str | None = None
    audit: tuple[dict, ...] = ()
    embedding_audit: dict | None = None

    @model_validator(mode='after')
    def validate_terminal(self):
        if self.status == 'failed':
            if not self.error or any(v is not None for v in (self.retrieval_text, self.embedding, self.evidence, self.reasoning_summary)):
                raise ValueError('失败任务必须有错误信息，不发布半成品记忆')
        else:
            if self.error is not None or not self.evidence or not self.reasoning_summary:
                raise ValueError('已完成整理必须提供依据和整理理由，不得带错误')
            if (self.status == 'summarized') != (self.retrieval_text is not None):
                raise ValueError('只有summarized状态有记忆正文')
            if (self.status == 'summarized') != (self.embedding is not None):
                raise ValueError('summarized必须同时具有正文和有效向量')
        return self


class MemoryPendingRecord(MemoryTaskInput):
    """交给Service的待入库内容；原身份由Service映射，不伪造会话、序号或入库时间。"""

    retrieval_text: str | None = Field(default=None, min_length=1, max_length=6000)
    embedding: MemoryEmbedding | None = None

    @model_validator(mode='after')
    def require_pair(self):
        if (self.retrieval_text is None) != (self.embedding is None):
            raise ValueError('待入库的检索文本和向量必须同时为空或同时存在')
        if self.retrieval_text is not None and not self.retrieval_text.strip():
            raise ValueError('待入库检索文本不能为空白')
        return self


class MemoryGenerationOutput(BaseModel):
    """整理向量与待入库内容；返回成功不等于已经写入数据库。"""

    model_config = ConfigDict(extra='forbid', frozen=True)

    execution_status: Literal['not_implemented', 'planned', 'summarized', 'partial_failed', 'failed'] = Field(default='not_implemented', description='planned仅筛选完成；summarized全部加工完成（有正文者已向量化）；partial_failed部分加工失败；failed筛选失败或全部已选任务加工失败。均未落盘。')
    plans: MemorySelectionResult | None = Field(default=None, description='完整筛选计划，后续整理失败不撤销该计划。')
    results: tuple[MemoryTaskResult, ...] | None = Field(default=None, description='按原任务顺序汇总的已选任务终态；未执行整理为null，零选择为[]。')
    pending_records: tuple[MemoryPendingRecord, ...] | None = Field(default=None, description='节点3组装的待入库内容，按原输入顺序含成功与跳过任务，不含加工失败任务；尚未汇总为null。')
    error: str | None = None
    planning_audit: tuple[dict, ...] = Field(default=(), description='私有逐轮审计，不进入模型上下文或前端历史。')

    @model_validator(mode='after')
    def validate_result(self):
        if self.execution_status in ('planned', 'summarized', 'partial_failed') and self.plans is None:
            raise ValueError('筛选成功后必须有完整plans')
        if (self.execution_status in ('failed', 'partial_failed')) != (self.error is not None):
            raise ValueError('失败或部分失败必须返回error')
        if self.results is not None:
            if self.plans is None:
                raise ValueError('整理结果必须关联完整plans')
            expected = [(t.task_id, t.task_number) for t in self.plans.selected_tasks]
            if [(t.task_id, t.task_number) for t in self.results] != expected:
                raise ValueError('整理结果必须完整、唯一且按计划顺序返回')
            failures = sum(r.status == 'failed' for r in self.results)
            status = ('failed' if failures and failures == len(self.results)
                      else 'partial_failed' if failures else 'summarized')
            if self.execution_status != status:
                raise ValueError('批次状态与整理结果不一致')
            if self.pending_records is None:
                raise ValueError('汇总终态必须包含待入库列表')
            ids = [r.task_id for r in self.pending_records]
            ready = {r.task_id: r for r in self.pending_records}
            allowed = {r.task_id for r in self.results if r.status != 'failed'} | set(self.plans.skipped_task_ids)
            if len(ids) != len(set(ids)) or set(ids) != allowed:
                raise ValueError('待入库列表必须完整覆盖成功与跳过任务，不得包含失败任务')
            for r in self.results:
                if r.status != 'failed' and (ready[r.task_id].retrieval_text, ready[r.task_id].embedding) != (r.retrieval_text, r.embedding):
                    raise ValueError('待入库正文或向量与加工结果不一致')
            if any(ready[tid].retrieval_text is not None for tid in self.plans.skipped_task_ids):
                raise ValueError('未选任务不得伪造检索内容')
        elif self.execution_status in ('summarized', 'partial_failed') or (
            self.execution_status == 'failed' and self.plans is not None
        ):
            raise ValueError('整理终态必须包含results')
        if self.execution_status == 'not_implemented' and self.plans is not None:
            raise ValueError('未运行时不能包含plans')
        if self.results is None and self.pending_records is not None:
            raise ValueError('尚未汇总不能包含待入库内容')
        return self


class ConversationMemoryInputState(TypedDict):
    request: MemoryGenerationInput


class ConversationMemoryOutputState(TypedDict):
    execution_status: Literal['not_implemented', 'planned', 'summarized', 'partial_failed', 'failed']
    plans: MemorySelectionResult | None
    results: tuple[MemoryTaskResult, ...] | None
    pending_records: tuple[MemoryPendingRecord, ...] | None
    error: str | None
    planning_audit: tuple[dict, ...]


class ConversationMemoryState(ConversationMemoryInputState, ConversationMemoryOutputState):
    """并行分支仅追加各自终态，节点3再校验并排序；不共享模型messages。"""

    task_results: Annotated[list[MemoryTaskResult], operator.add]


class MemoryWorkerState(TypedDict):
    task: MemoryTaskInput
    selection: SelectedMemoryTask


__all__ = [
    'MemoryTaskInput', 'MemoryGenerationInput', 'MemoryGenerationOutput',
    'ConversationMemoryInputState', 'ConversationMemoryOutputState', 'ConversationMemoryState',
    'MemoryTaskResult', 'MemoryWorkerState', 'MemoryPendingRecord',
]
