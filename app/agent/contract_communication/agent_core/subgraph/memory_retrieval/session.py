"""一次记忆检索的独立条件容器；六个方法通过检索子Agent工具适配调用。"""
from datetime import date as calendar_date, datetime, time, timedelta, timezone
from typing import Literal

from pydantic import Field, model_validator

from .schema import (
    FieldQuery, MemoryRetrievalRequest, MemoryRetrievalResult, RetrievalModel,
    RetrievalPlan,
)

TaskEndStatus = Literal['completed', 'cancelled', 'superseded', 'rejected', 'failed', 'expired']
_QUERY_FIELDS = ('user_input', 'intermediate_output', 'final_output')
_BEIJING = timezone(timedelta(hours=8))


class RetrievalConditions(RetrievalModel):
    """允许尚未选择字段的编辑状态；正式执行时再校验至少一个字段。"""
    created_from: int | None = Field(default=None, ge=0, strict=True, description='任务创建时间下界，UTC Unix毫秒，包含边界；null表示不限。')
    created_before: int | None = Field(default=None, ge=0, strict=True, description='任务创建时间上界，UTC Unix毫秒，不含边界；null表示不限。')
    statuses: tuple[TaskEndStatus, ...] | None = Field(default=None, description='限定的任务终态；null表示不限，不允许空列表或重复状态。')
    user_input: str | None = Field(default=None, min_length=1, description='用户输入字段的查询正文；null表示不启用该字段。')
    intermediate_output: str | None = Field(default=None, min_length=1, description='中途输出字段的查询正文；null表示不启用该字段。')
    final_output: str | None = Field(default=None, min_length=1, description='最终结论字段的查询正文；null表示不启用该字段。')

    @model_validator(mode='after')
    def validate_filters(self):
        if self.created_from is not None and self.created_before is not None and self.created_from >= self.created_before:
            raise ValueError('时间下界必须小于上界')
        if self.statuses is not None and (not self.statuses or len(set(self.statuses)) != len(self.statuses)):
            raise ValueError('任务终态不能是空集合或包含重复项；清除限制请传null')
        return self


class MemoryRetrievalSession:
    """每次检索调用创建一个实例，不跨调用共享查询条件或执行结果。"""

    def __init__(self, request: MemoryRetrievalRequest | dict, *, database=None, encoder=None, result_pool=None):
        self._request = MemoryRetrievalRequest.model_validate(request)
        self._result_pool = result_pool
        self._database = database
        self._encoder = encoder
        self._executing = False
        self._conditions = RetrievalConditions()
        self._result: MemoryRetrievalResult | None = None

    @property
    def request(self) -> MemoryRetrievalRequest:
        return self._request

    @property
    def conditions(self) -> RetrievalConditions:
        return self._conditions

    @property
    def result(self) -> MemoryRetrievalResult | None:
        """仅保留成功执行的正式结果；失败不作为终止标志。"""
        return self._result

    def _replace(self, **changes) -> RetrievalConditions:
        if self._executing:
            raise ValueError('查询正在执行，不能修改条件')
        if self._result is not None:
            raise ValueError('查询已经成功结束，不能继续修改条件')
        # 完整校验候选副本后再提交，失败时原条件保持不变。
        candidate = RetrievalConditions.model_validate({**self._conditions.model_dump(), **changes})
        self._conditions = candidate
        return candidate

    def set_time_filter(
        self, *, date: str | None = None,
        created_from: int | None = None, created_before: int | None = None,
    ) -> RetrievalConditions:
        """设置单个北京时间自然日或UTC毫秒范围；全部为null时清除时间限制。"""
        if date is not None:
            if created_from is not None or created_before is not None:
                raise ValueError('单个日期和时间范围不能同时指定')
            try:
                day = calendar_date.fromisoformat(date)
                if day.isoformat() != date:
                    raise ValueError('日期格式不正确')
                start = datetime.combine(day, time.min, tzinfo=_BEIJING)
                end = start + timedelta(days=1)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError('日期必须为有效的YYYY-MM-DD且可计算次日边界') from exc
            created_from = int(start.timestamp() * 1000)
            created_before = int(end.timestamp() * 1000)
        return self._replace(created_from=created_from, created_before=created_before)

    def set_status_filter(self, statuses: tuple[TaskEndStatus, ...] | None = None) -> RetrievalConditions:
        """替换任务终态限制；null清除限制，不能传入进行中等非终态。"""
        return self._replace(statuses=statuses)

    def set_user_input_query(self, query: str | None) -> RetrievalConditions:
        """设置用户输入查询文本；再次设置替换原值，null取消该字段召回。"""
        return self._replace(user_input=query)

    def set_intermediate_output_query(self, query: str | None) -> RetrievalConditions:
        """设置中途输出查询文本；再次设置替换原值，null取消该字段召回。"""
        return self._replace(intermediate_output=query)

    def set_final_output_query(self, query: str | None) -> RetrievalConditions:
        """设置最终结论查询文本；再次设置替换原值，null取消该字段召回。"""
        return self._replace(final_output=query)

    async def execute_query(self) -> MemoryRetrievalResult:
        """冻结合法条件后执行并发召回与RRF；成功保存正式结果，失败保留原条件。"""
        if self._result is not None:
            return self._result
        conditions = self._conditions
        queries = tuple(FieldQuery(field=field, query=getattr(conditions, field))
                        for field in _QUERY_FIELDS if getattr(conditions, field) is not None)
        if not queries:
            return MemoryRetrievalResult(status='error', error_code='missing_query',
                                         error='请至少设置用户输入、中途输出或最终结论中的一个检索文本。')
        plan = RetrievalPlan(queries=queries, created_from=conditions.created_from,
                             created_before=conditions.created_before, statuses=conditions.statuses)
        from pathlib import Path
        from app.core.config import get_settings
        from .query import run_query, embed_query
        import logging
        if self._executing:
            return MemoryRetrievalResult(status='error', error_code='query_busy', error='查询正在执行，请等待结果。')
        database = self._database or get_settings().communication_database_file
        database = Path(database)
        if not database.is_absolute():
            database = Path(__file__).resolve().parents[6] / database
        self._executing = True
        try:
            result = await run_query(self._request, plan, database=database, encoder=self._encoder or embed_query)
            if not result.tasks:
                # 空召回是成功终态，但不创建引用、不占用或驱逐已有驻留结果。
                self._result = MemoryRetrievalResult(status='success', total=0, total_pages=0)
                return self._result
            from .pool import MemoryQueryPool
            if self._result_pool is None:
                self._result_pool = MemoryQueryPool(self._request.conversation_id, database)
            if self._result_pool.conversation_id != self._request.conversation_id or self._result_pool.database.resolve() != database.resolve():
                raise ValueError('查询结果池的会话或数据库不一致')
            query_id = self._result_pool.add(result)
            # 会话实例只保留轻量回执，不驻留任务轨迹或页面文本。
            self._result = MemoryRetrievalResult(status='success', query_id=query_id, total=len(result.tasks),
                total_pages=max(1,(len(result.tasks)+self._result_pool.page_size-1)//self._result_pool.page_size))
            return self._result
        except Exception:
            logging.getLogger(__name__).exception('记忆查询执行失败')
            return MemoryRetrievalResult(status='error', error_code='query_execution_failed',
                error='记忆查询执行失败，请检查数据库、分词扩展及Embedding服务；不要通过修改检索条件规避服务故障。')
        finally:
            self._executing = False

    async def view_query(self, query_id: str, page: int | None = None) -> dict:
        """读取当前实例绑定结果池中的指定页或下一页；不重新执行检索。"""
        if self._result_pool is None:
            return {'status':'error','error_code':'query_expired','message':'查询结果不存在或已释放，请重新检索。'}
        return await self._result_pool.view(query_id, page)
