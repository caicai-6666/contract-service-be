"""一次子图调用的候选池：仅保存ID和分数，不提供任何view工具。"""
import asyncio
from collections import OrderedDict
from uuid import uuid4
from app.infrastructure.contract_metadata_store import ContractMetadataStatus
from app.agent.contract_communication.agent_core.tool.file_viewer import FileViewError
from .schema import ContractCandidate,ContractRetrievalResult
from ...contract_ranking import CandidateRanking, rank_candidates, union_candidates

class CandidatePool:
    def __init__(self,metadata,*,initial_document_ids=None,initial_ranking=None,capacity=10,rrf_k=60,history_weight=0.5):
        self.metadata=metadata
        self.initial=initial_document_ids
        self.rrf_k=rrf_k; self.history_weight=history_weight
        self.initial_ranking=None
        if initial_ranking is not None:
            rows=tuple(initial_ranking)
            if not rows or initial_document_ids is None or tuple(r.document_id for r in rows) != tuple(initial_document_ids):
                raise ValueError('初始排名与范围不一致')
            if len({(r.search_type,r.score_kind,r.has_relevance) for r in rows}) != 1:
                raise ValueError('初始排名元数据不一致')
            self.initial_ranking=CandidateRanking(tuple((r.document_id,r.score) for r in rows),
                tuple((r.document_id,r.raw_score if r.raw_score is not None else r.score) for r in rows),
                rows[0].search_type,rows[0].score_kind,rows[0].has_relevance)
        self.capacity=capacity
        self._results=OrderedDict()
        self._closed=False

    def close(self):
        self._closed=True;self._results.clear()

    def document_ids(self,result_id):
        if self._closed:raise FileViewError('session_closed','候选查询已结束。')
        if result_id is None:return self.initial
        entry=self._results.get(result_id)
        if entry is None:raise FileViewError('result_unavailable','候选结果集不存在或已驱逐，请重新查询。')
        self._results.move_to_end(result_id)
        return tuple(doc for doc,_ in entry.hits)

    def put(self,hits,*,search_type,note_ids=None,parent_result_id=None,exact=False):
        if self._closed:raise FileViewError('session_closed','候选查询已结束。')
        hits=tuple(hits)
        if not hits:raise ValueError('空结果不创建ID')
        if self.initial is not None and any(doc not in self.initial for doc,_ in hits):raise ValueError('结果越过初始范围')
        parent=self.initial_ranking
        if parent_result_id is not None:
            self.document_ids(parent_result_id)
            parent=self._results[parent_result_id]
        ranked=rank_candidates(hits,search_type=search_type,parent=parent,exact=exact,
            k=self.rrf_k,history_weight=self.history_weight)
        rid='candidate-set:'+str(uuid4())
        self._results[rid]=ranked
        while len(self._results)>self.capacity:self._results.popitem(last=False)
        return rid

    def filter(self,document_ids,*,parent_result_id=None):
        """预留精确筛选入口；空结果由工具返回，不创建空集合。"""
        return self.put(tuple((doc,1.0) for doc in document_ids),search_type='exact',
            parent_result_id=parent_result_id,exact=True)

    def union(self, left_result_id, right_result_id):
        """两个本地引用合并后另建集合；先验证两表，失败不发布半成品。"""
        self.document_ids(left_result_id)
        self.document_ids(right_result_id)
        if left_result_id == right_result_id:
            return left_result_id
        ranked = union_candidates(self._results[left_result_id], self._results[right_result_id], k=self.rrf_k)
        if self.initial is not None and any(doc not in self.initial for doc, _ in ranked.hits):
            raise ValueError('并集越过初始范围')
        rid = 'candidate-set:' + str(uuid4())
        self._results[rid] = ranked
        while len(self._results) > self.capacity:
            self._results.popitem(last=False)
        return rid

    async def finish(self,args):
        if self._closed:raise ValueError('候选查询已结束')
        if args.result_id is None:
            return ContractRetrievalResult(status='success',explanation=args.explanation or '没有找到合适的合同。')
        self.document_ids(args.result_id)
        entry=self._results[args.result_id]
        def read():
            candidates=[]
            for doc,score in entry.hits:
                m=self.metadata.get(doc)
                if m is not None and m.status is ContractMetadataStatus.READY:
                    candidates.append(ContractCandidate(document_id=doc,file_name=m.file_name,summary=m.summary,score=score,search_type=entry.search_type,raw_score=dict(entry.raw_hits)[doc],score_kind=entry.score_kind,has_relevance=entry.has_relevance))
            return tuple(candidates)
        candidates=await asyncio.to_thread(read)
        if self._closed:raise ValueError('候选查询已结束')
        return ContractRetrievalResult(status='success',candidates=candidates,explanation=args.explanation)
