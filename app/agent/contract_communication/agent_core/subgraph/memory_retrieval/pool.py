"""会话级查询LRU：仅驻留任务身份/排名；每次翻页实时读库和渲染。"""
import asyncio
from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from uuid import uuid4

from pydantic import Field
from .schema import RetrievalModel, MemoryRetrievalResult
from .tasks import MemoryTaskPage, pull_ranked_tasks
from .rendering import render_memory_task_page



@dataclass
class QueryEntry:
    rankings: tuple[tuple[str, float], ...]
    current_page: int = 0


class MemoryQueryPool:
    """单事件循环中由一个驻留会话持有；不同会话不得共享实例。"""
    def __init__(self, conversation_id: str, database: Path, *, capacity: int = 10, page_size: int = 3):
        if type(capacity) is not int or capacity < 1 or type(page_size) is not int or page_size < 1:
            raise ValueError('容量和每页条数必须为正整数')
        self.conversation_id, self.database = conversation_id, Path(database)
        self.capacity, self.page_size = capacity, page_size
        self._entries = OrderedDict()
        self._view_lock = asyncio.Lock()
        self._closed = False

    def add(self, result: MemoryRetrievalResult) -> str:
        if self._closed or result.status != 'success':
            raise ValueError('结果池已释放或查询未成功')
        rankings = tuple((t.record.record_id,t.rrf_score) for t in result.tasks)
        if not rankings:
            raise ValueError('空结果不创建查询标识')
        if len({i for i,_ in rankings}) != len(rankings) or any(a[1]<b[1] for a,b in zip(rankings,rankings[1:])):
            raise ValueError('结果必须是去重后的降序任务列表')
        query_id = str(uuid4())
        self._entries[query_id] = QueryEntry(rankings)
        while len(self._entries)>self.capacity:
            self._entries.popitem(last=False)
        return query_id

    def close(self):
        self._closed = True
        self._entries.clear()

    def _read(self, rankings):
        if not rankings:
            return ()
        with closing(sqlite3.connect(self.database.resolve().as_uri()+'?mode=ro', uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            ids = [i for i,_ in rankings]
            rows = connection.execute("SELECT * FROM conversation_records WHERE conversation_id=? AND kind='task' AND record_id IN ("
                + ','.join('?' for _ in ids)+')', [self.conversation_id,*ids]).fetchall()
        return pull_ranked_tasks([dict(r) for r in rows],rankings)

    async def view(self, query_id: str, page: int | None = None, *, advance: bool = True) -> dict:
        async with self._view_lock:
            entry = self._entries.get(query_id) if isinstance(query_id,str) else None
            if self._closed or entry is None:
                return {'status':'error','error_code':'query_expired','message':'查询结果不存在或已释放，请重新检索获取新的query_id。'}
            self._entries.move_to_end(query_id)
            total = len(entry.rankings)
            pages = max(1,(total+self.page_size-1)//self.page_size)
            target = entry.current_page+1 if page is None else page
            if page is None and target>pages:
                return {'status':'error','error_code':'end_of_results','query_id':query_id,
                        'message':f'已到最后一页（第{pages}页，共{total}条），可指定页码重新查看。'}
            if type(target) is not int or not 1<=target<=pages:
                return {'status':'error','error_code':'invalid_page','query_id':query_id,
                        'message':f'页码无效，请输入1至{pages}之间的整数；省略页码可查看下一页。'}
            start = (target-1)*self.page_size
            try:
                tasks = await asyncio.to_thread(self._read,entry.rankings[start:start+self.page_size])
                data = MemoryTaskPage(query_id=query_id,page=target,page_size=self.page_size,total=total,
                    total_pages=pages,start_rank=start+1 if total else 0,tasks=tasks)
                content = render_memory_task_page(data)
            except ValueError:
                return {'status':'error','error_code':'task_unavailable','message':'本页部分历史任务已不存在或无法读取，请重新检索；页码未推进。'}
            except Exception:
                return {'status':'error','error_code':'read_failed','message':'历史任务暂时读取失败，请稍后重试；页码未推进。'}
            # 在读取期间会话可能被释放，或新查询导致该对象被LRU驱逐。
            if self._closed or self._entries.get(query_id) is not entry:
                return {'status':'error','error_code':'query_expired','message':'查询结果已释放，请重新检索。'}
            if advance:
                entry.current_page = target
            self._entries.move_to_end(query_id)
            return {'status':'success','query_id':query_id,'page':target,'total_pages':pages,
                    'total':total,'has_next':target<pages,'content':content}

async def view_memory_query(pool: MemoryQueryPool, arguments: dict | str) -> dict:
    """查看方法的参数适配；与检索子Agent的六个建模工具分开。"""
    from ...tool.memory_viewer import ViewMemoryQueryArguments
    from app.infrastructure.model_json import load_model_json, validate_model_payload
    try:
        args = validate_model_payload(ViewMemoryQueryArguments,
            load_model_json(arguments) if isinstance(arguments,str) else arguments)
    except (ValueError, TypeError):
        return {'status':'error','error_code':'invalid_arguments',
                'message':'请提供完整query_id，页码应为整数；省略或null表示下一页。'}
    return await pool.view(args.query_id,args.page)
