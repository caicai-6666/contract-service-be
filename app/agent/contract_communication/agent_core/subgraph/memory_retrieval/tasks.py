"""按召回顺序拉取快照中的原任务并分页；不重新查询、不管理LRU。"""
import json
from typing import Iterable

from pydantic import Field
from app.schema.communication import ConversationHistoryRecord
from .schema import RetrievalModel, RankedTask, MemoryRetrievalResult


def pull_ranked_tasks(rows: Iterable[dict], rankings: Iterable[tuple[str, float]]) -> tuple[RankedTask, ...]:
    """仅在已过滤的候选快照中回连任务；缺失/重复标识失败，不静默改变排名。"""
    by_id = {row['record_id']: row for row in rows}
    tasks, seen = [], set()
    for record_id, score in rankings:
        if record_id in seen or record_id not in by_id:
            raise ValueError('排名中存在重复或不属于候选快照的任务')
        seen.add(record_id)
        row = by_id[record_id]
        record = ConversationHistoryRecord.model_validate({
            key: (json.loads(row[key]) if key == 'payload' else row[key])
            for key in ('record_id','sequence','kind','turn_id','status','payload','created_at',
                        'activated_at','processing_duration_ms')})
        tasks.append(RankedTask(record=record, rrf_score=score))
    return tuple(tasks)


class MemoryTaskPage(RetrievalModel):
    query_id: str | None = Field(default=None, description='调用方持有的完整查询标识；尚未分配时为null。')
    page: int = Field(ge=1, description='从1开始的当前页码。')
    page_size: int = Field(ge=1, description='每页最多展示的任务数量。')
    total: int = Field(ge=0, description='本次查询实际保留的召回任务数，不是数据库全部匹配数。')
    total_pages: int = Field(ge=1, description='页数；空结果仍有第1页。')
    start_rank: int = Field(ge=0, description='本页首条全局排名，空结果为0。')
    tasks: tuple[RankedTask, ...] = Field(description='本页按原召回排名排列的任务快照。')
