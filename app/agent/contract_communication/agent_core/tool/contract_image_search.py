"""全文件图像相似合同检索与结果分页；驻留池只保存 ID 和排序分数。"""
import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import hmac
import logging
import math
import re
import secrets
from typing import Literal
from ..contract_ranking import CandidateRanking, rank_candidates
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.pdf_deduplication.node import encode_pdf_pages
from app.infrastructure.contract_metadata_store import ContractMetadataStatus
from app.schema.agent_tool_content import FoldableToolContent, ToolPageReference
from ..subgraph.fifo_management.schema import FIFOExecutionResult
from .file_viewer import FileViewError
from .progress import ToolProgress
from .registry import RegisteredTool

logger = logging.getLogger(__name__)


class SearchContractsByImageArguments(BaseModel):
    """当你需要根据一份附件或合同的页面图像寻找相似合同、可能的不同版本或相近版式文档时，使用这个工具。使用完整 PDF 的全部页面进行融合检索，无需逐页打开。不是按自然语言条款进行语义检索，相似分数不能证明重复或法律内容一致。有结果时返回结果集ID和数量，请调用view_contract_search_results查看列表；零结果不生成ID，仅返回未找到提示，无需翻页。可用已有图片结果或最终合同候选结果限定范围，并融合历史排名与本轮图片排名；省略result_id搜索全库。以合同为源时排除其自身；附件不排除同内容的已入库合同。"""
    model_config = ConfigDict(extra='forbid', frozen=True)
    source_type: Literal['session', 'contract'] = Field(description='来源类型：session为当前会话已准入的附件，contract为已入库共享合同。不得将其他会话附件当作合同访问。')
    file_id: str = Field(description='完整文件标识：session使用附件file_id（UUID），contract使用合同document_id（64位小写SHA-256）。从用户输入或工具结果原样复制，禁止缩写、截断、使用文件名或结果集ID替代。')
    result_id: str | None = Field(default=None, min_length=1, description='可选的完整结果集ID，原样复制图片检索返回的contract-search:引用或合同检索最终返回的contract-query:引用；仅在该候选范围内检索并融合排名。省略或null搜索全库；不是源文件ID，不接受关系结果或其他会话引用，失效时需重新检索。')
    top_k: int = Field(default=10, ge=1, le=50, strict=True, description='最多返回的相似合同数量，默认10，范围1至50。结果是有限近邻候选，不表示全库符合条件合同的总数。')

    @model_validator(mode='after')
    def validate_file_id(self):
        if self.source_type == 'contract':
            if not re.fullmatch(r'[0-9a-f]{64}', self.file_id):
                raise ValueError('合同ID必须为完整64位小写SHA-256')
        elif str(UUID(self.file_id)) != self.file_id:
            raise ValueError('附件ID必须是完整规范UUID')
        return self


class ViewContractSearchResultsArguments(BaseModel):
    """当你需要查看合同检索命中的合同名称、完整ID、摘要和相似度时，使用这个工具。根据检索返回的结果集ID分页读取；省略页码首次第1页，后续下一页。备注检索还会展示命中备注、撰写人和时间。页面会注明图像相似、问题语义相关、名称匹配、摘要或备注综合排名，分数不证明合同存在某项具体约定，需查看原文核实。结果驻留在本会话，释放或驱逐后需重新检索。"""
    model_config = ConfigDict(extra='forbid', frozen=True)
    result_id: str = Field(min_length=1, description='检索成功返回的完整结果集ID，原样复制；不是合同ID。仅当前驻留会话可用，不可猜测、缩写或使用其他会话引用。')
    page: int | None = Field(default=None, strict=True, description='从1开始的页码；省略或null首次打开第1页，之后翻到下一页。显式指定页码重新查看，越界或到末尾不会移动游标。')


@dataclass
class _Results:
    hits: tuple[tuple[str, float], ...]
    search_type: Literal['image', 'question', 'notes', 'summary', 'name', 'clause', 'exact', 'union'] = 'image'
    raw_hits: tuple = ()
    score_kind: str = 'raw'
    has_relevance: bool = True
    page: int = 0
    anchor_contract_id: str | None = None
    note_ids: dict[str, str] = field(default_factory=dict)


def _error(code, message):
    return FIFOExecutionResult(status='failed', tool_result={'status':'error', 'code':code, 'message':message})


class ContractSearchResults:
    """每个驻留会话独立 LRU；不保存合同正文或渲染后的页面。"""
    render_note_evidence = True
    resource_prefix = 'contract-search:'
    page_title = '合同检索列表'

    def __init__(self, metadata_store, *, capacity=10, page_size=5):
        if type(capacity) is not int or capacity < 1 or type(page_size) is not int or page_size < 1:
            raise ValueError('结果池容量和页大小必须为正整数')
        self._metadata = metadata_store
        self.capacity, self.page_size = capacity, page_size
        self._results = OrderedDict()
        self._key = secrets.token_bytes(32)
        self._closed = False

    def close(self):
        self._closed = True
        self._results.clear()
        self._key = secrets.token_bytes(32)

    def put(self, hits, *, search_type: Literal['image', 'question', 'notes', 'summary', 'name', 'clause', 'exact', 'union'] = 'image', note_ids=None, parent_result_id=None, parent_ranking=None, rrf_k=60, history_weight=0.5):
        if self._closed:
            raise FileViewError('session_closed', '会话已释放，请重新发起检索。')
        hits = tuple(hits)
        if not hits:
            raise ValueError('空结果不创建结果集ID')
        if search_type not in ('image', 'question', 'notes', 'summary', 'name', 'clause', 'exact', 'union'):
            raise ValueError('未知检索类型')
        if parent_result_id is not None and parent_ranking is not None:
            raise ValueError('父结果引用与排名快照不能同时提供')
        parent = parent_ranking
        if parent_result_id is not None:
            self.document_ids(parent_result_id)
            entry = self._results[parent_result_id]
            parent = CandidateRanking(entry.hits, entry.raw_hits, entry.search_type, entry.score_kind, entry.has_relevance)
        ranked = rank_candidates(hits, search_type=search_type, parent=parent, k=rrf_k, history_weight=history_weight)
        result_id = self.resource_prefix + str(uuid4())
        self._results[result_id] = _Results(ranked.hits, search_type=search_type,
            raw_hits=ranked.raw_hits, score_kind=ranked.score_kind, has_relevance=ranked.has_relevance,
            note_ids=dict(note_ids or {}))
        while len(self._results) > self.capacity:
            self._results.popitem(last=False)
        return result_id

    def ranking_state(self, result_id):
        """校验驻留引用并返回不可变排名，供其他结果池继承。"""
        self.document_ids(result_id)
        entry = self._results[result_id]
        return CandidateRanking(entry.hits, entry.raw_hits or entry.hits,
            entry.search_type, entry.score_kind, entry.has_relevance)

    def ranking_snapshot(self, result_id):
        """只传递候选分数元数据，跨子图调用不丢失历史相关性。"""
        self.document_ids(result_id)
        entry = self._results[result_id]
        raw = dict(entry.raw_hits or entry.hits)
        return tuple({'document_id':doc, 'score':score, 'raw_score':raw[doc],
            'search_type':entry.search_type, 'score_kind':entry.score_kind,
            'has_relevance':entry.has_relevance} for doc, score in entry.hits)

    def document_ids(self, result_id: str) -> tuple[str, ...]:
        """获取当前会话的不可变候选范围，不消费分页游标、不展开给模型。"""
        if result_id is None and not self._closed:
            return None
        entry = self._results.get(result_id)
        if self._closed or entry is None:
            raise FileViewError('result_unavailable', '结果集不存在、已被驱逐或会话已释放，请重新检索。')
        self._results.move_to_end(result_id)
        return tuple(doc for doc, _ in entry.hits)

    def _sign(self, resource, locator, nonce):
        return hmac.new(self._key, f'{resource}\n{locator}\n{nonce}'.encode(), hashlib.sha256).hexdigest()

    async def view(self, result_id, page=None):
        entry = self._results.get(result_id)
        if self._closed or entry is None:
            return _error('result_unavailable', '结果集不存在、已被驱逐或会话已释放，请重新检索。')
        target = page if page is not None else entry.page + 1
        total = len(entry.hits)
        pages = (total + self.page_size - 1)//self.page_size
        if page is not None and (page < 1 or page > max(1, pages)):
            return _error('invalid_page', f'页码无效，当前结果共{pages}页。')
        if total == 0:
            return FIFOExecutionResult(status='succeeded', tool_result={'status':'success', 'result_id':result_id, 'count':0, 'total_pages':0, 'message':'本次检索没有候选合同。'})
        if target > pages:
            return _error('end_of_results', f'已到最后一页（共{pages}页），可指定页码重新查看。')
        entry.page = target
        self._results.move_to_end(result_id)
        locator, nonce = str(target), uuid4().hex
        reference = ToolPageReference(resource_id=result_id, locator=locator,
            display_id=nonce+'.'+self._sign(result_id, locator, nonce), media_type='text',
            description=f'{self.page_title}，第{target}/{pages}页，共{total}条候选')
        return FIFOExecutionResult(status='succeeded', tool_result={'status':'success', 'result_id':result_id,
            'page':target, 'total_pages':pages, 'count':total}, content=FoldableToolContent(pages=[reference]))

    async def resolve_page(self, reference):
        entry = self._results.get(reference.resource_id)
        if self._closed or entry is None:
            return [{'type':'text', 'text':'合同检索结果已释放或被驱逐，请重新检索。'}]
        try:
            nonce, signature = reference.display_id.split('.', 1)
            page = int(reference.locator)
            if reference.media_type != 'text' or not hmac.compare_digest(signature, self._sign(reference.resource_id, reference.locator, nonce)):
                raise ValueError('signature')
            if not 1 <= page <= (len(entry.hits)+self.page_size-1)//self.page_size:
                raise ValueError('page')
        except (ValueError, AttributeError) as exc:
            raise FileViewError('invalid_reference', '合同检索页面引用无效，请重新查看。') from exc
        hits = entry.hits[(page-1)*self.page_size:page*self.page_size]
        def read():
            rows = []
            for doc, score in hits:
                metadata = self._metadata.get(doc)
                note = None
                if self.render_note_evidence and entry.search_type == 'notes' and metadata is not None and metadata.status is ContractMetadataStatus.READY:
                    note = self._metadata.get_note(doc, entry.note_ids.get(doc))
                rows.append((doc, score, metadata, note))
            return rows
        try:
            rows = await asyncio.to_thread(read)
        except Exception:
            logger.exception('合同检索结果读取失败')
            return [{'type':'text', 'text':'合同检索结果读取失败，请稍后重试，不能据此判断合同不存在。'}]
        if self._closed or self._results.get(reference.resource_id) is not entry:
            return [{'type':'text', 'text':'合同检索结果已释放，请重新检索。'}]
        pages = (len(entry.hits)+self.page_size-1)//self.page_size
        label = {'image':'图像余弦相似度','question':'问题语义相似度','notes':'备注综合检索分数（RRF）','summary':'摘要综合检索分数（RRF）','name':'名称匹配分数（BM25）','clause':'条款匹配分数（BM25，最高条款分）','exact':'精确过滤标记分','union':'并集排名分数（RRF）'}[entry.search_type]
        explanation = ('相似不代表重复或条款一致。' if entry.search_type == 'image'
                       else '表示合同可能回答相关问题，不代表已确认具体约定。')
        if entry.search_type == 'clause':
            explanation = '条款文字命中不等于约定已核实；BM25分数不是概率，需核对否定、前提与例外。'
        if entry.search_type == 'name':
            explanation = '仅表示名称词项匹配，分数不是概率，不能证明合同具体约定。'
        if entry.search_type == 'summary':
            explanation = '摘要可能省略细节，需核对合同原文；RRF分数不是相似度或概率。'
        if entry.search_type == 'notes':
            explanation = '备注是用户记录，不代表已核实的合同事实；RRF分数不是相似度或概率。'
        if entry.search_type == 'union':
            explanation = '并集排名按两表等权融合；零分表示仅由纯筛选召回，没有相关性排名贡献，仍是有效候选。'
        raw_label = label
        if entry.score_kind == 'rrf':
            label = '跨查询综合排序分数（RRF）'
        if not entry.has_relevance:
            explanation = '仅按确定性顺序展示，不代表相关性排名。'
        lines = ['# 合同检索结果', f'结果集 ID：{reference.resource_id}',
                 f'第 {page} / {pages} 页 · 共 {len(entry.hits)} 条候选',
                 f'按{label}降序排列；{explanation}']
        for rank, (doc, score, metadata, note) in enumerate(rows, (page-1)*self.page_size+1):
            if metadata is None or metadata.status is not ContractMetadataStatus.READY:
                lines += ['', f'## {rank}. 合同已不可用', f'合同 ID：{doc}']
            else:
                lines += ['', f'## {rank}. {metadata.file_name}', f'合同 ID：{doc}',
                          f"{label}：{format(score, '.6g' if entry.search_type in ('name', 'clause', 'exact', 'union') else '.6f')}", '摘要：', metadata.summary or '未记录']
                if entry.score_kind == 'rrf' and entry.search_type != 'union':
                    lines.append(f'本轮原始{raw_label}：{dict(entry.raw_hits)[doc]:.6g}')
                if self.render_note_evidence and entry.search_type == 'notes':
                    if note is None:
                        lines += ['命中备注已删除或不可用，请重新检索。']
                    else:
                        lines += ['命中备注（用户记录）：', note['content'], f"撰写人：{note['author_name']}", f"时间：{note['created_at']}"]
        lines += ['', '已到最后一页。' if page == pages else f'可继续查看第 {page+1} 页。']
        self._results.move_to_end(reference.resource_id)
        return [{'type':'text', 'text':'\n'.join(lines)}]


def build_contract_image_search_registrations(*, session_viewer, contract_viewer, results,
                                               client, index_name, metadata_store, settings, authorize, final_results=None):
    async def search(operation, arguments):
        try:
            await authorize()
            parent_pool = None
            scope = None
            if arguments.result_id is not None:
                parent_pool = next((pool for pool in (results, final_results)
                    if pool is not None and arguments.result_id.startswith(pool.resource_prefix)), None)
                if parent_pool is None:
                    raise FileViewError('result_unavailable', '请使用当前会话的图片结果或最终合同候选结果集ID。')
                scope = frozenset(parent_pool.document_ids(arguments.result_id))
            if arguments.source_type == 'contract':
                source = await asyncio.to_thread(metadata_store.get, arguments.file_id)
                if source is None or source.status is not ContractMetadataStatus.READY:
                    return _error('file_unavailable', '源合同不存在或尚未就绪，请核对合同ID。')
            viewer = session_viewer if arguments.source_type == 'session' else contract_viewer
            descriptor, pages = await viewer.snapshot_pages(arguments.file_id)
            vector = await encode_pdf_pages(pages, settings=settings.embedding)
            await authorize()
            if parent_pool is not None:
                parent_pool.document_ids(arguments.result_id)
            # 预取较多候选，随后依据 SQLite ready 状态过滤，不以 ES 记录单独认定可见性。
            limit = min(arguments.top_k * 4, 200)
            filters = {'bool': {'filter':[{'exists':{'field':'vectors.page_fusion'}}]}}
            if scope is not None:
                filters['bool']['filter'].append({'ids':{'values':sorted(scope)}})
            if arguments.source_type == 'contract':
                filters['bool']['must_not'] = [{'ids':{'values':[arguments.file_id]}}]
            knn = {'field':'vectors.page_fusion', 'query_vector':list(vector), 'k':limit,
                   'num_candidates':max(100, limit*2), 'filter':filters}
            # 与合同提取查重共用召回门槛；它不是最终重复判定，且先于排名融合。
            minimum_similarity = settings.pdf_deduplication.minimum_recall_cosine_similarity
            knn['similarity'] = minimum_similarity
            response = await client.search(index=index_name, knn=knn, size=limit, source=['document_id'])
            if response.get('timed_out') or response.get('_shards', {}).get('failed', 0):
                raise RuntimeError('ES 查询未完整成功')
            def collect():
                hits, seen = [], set()
                for hit in response['hits']['hits']:
                    doc = hit['_source']['document_id']
                    score = float(hit['_score'])*2-1
                    if not re.fullmatch(r'[0-9a-f]{64}', doc) or not math.isfinite(score):
                        raise ValueError('ES 返回非法合同或分数')
                    # 防御性校验服务响应，绝不扩大显式指定的候选范围。
                    if scope is not None and doc not in scope:
                        continue
                    if score < minimum_similarity:
                        continue
                    if doc in seen or (arguments.source_type == 'contract' and doc == arguments.file_id):
                        continue
                    seen.add(doc)
                    metadata = metadata_store.get(doc)
                    if metadata is not None and metadata.status is ContractMetadataStatus.READY:
                        hits.append((doc, max(-1.0, min(1.0, score))))
                return sorted(hits, key=lambda item:(-item[1], item[0]))[:arguments.top_k]
            hits = await asyncio.to_thread(collect)
            await authorize()
            # 异步编码/查询期间父集合可能被驱逐；发布前再次校验，禁止回退全库。
            parent = parent_pool.ranking_state(arguments.result_id) if parent_pool is not None else None
            if not hits:
                return FIFOExecutionResult(status='succeeded', tool_result={'status':'success', 'count':0,
                    'source_page_count':len(pages), 'top_k':arguments.top_k,
                    'message':'本次检索未找到符合条件的相似合同，未生成结果集ID，无需调用翻页工具。'})
            result_id = results.put(hits, parent_ranking=parent,
                rrf_k=settings.communication_contract_retrieval_rrf_k,
                history_weight=settings.communication_contract_retrieval_history_weight)
            return FIFOExecutionResult(status='succeeded', tool_result={'status':'success', 'result_id':result_id,
                'count':len(hits), 'source_page_count':len(pages), 'top_k':arguments.top_k,
                'message':'查询成功；结果为有限近邻候选，并非全库完整清单。请使用view_contract_search_results分页查看。'})
        except FileViewError as exc:
            return _error(exc.code, str(exc))
        except PermissionError:
            return _error('file_unavailable', '会话或文件访问权限已失效，请重新发起检索。')
        except Exception:
            logger.exception('合同图像检索失败')
            return _error('search_failed', '图像编码或合同检索失败，请稍后重试；不能据此判断没有相似合同。')

    return (
        RegisteredTool('search_contracts_by_image', SearchContractsByImageArguments.__doc__, SearchContractsByImageArguments,
                       search, progress=ToolProgress('local-search', '正在查阅相似合同')),
        build_contract_search_view_registration(results),
    )


def build_contract_search_view_registration(results):
    """共享分页独立注册，SQLite 备注检索不依赖 ES 是否启用。"""
    async def view(operation, arguments):
        return await results.view(arguments.result_id, arguments.page)
    return RegisteredTool('view_contract_search_results', ViewContractSearchResultsArguments.__doc__+f'每页{results.page_size}条。',
        ViewContractSearchResultsArguments, view, return_types=('ordinary','foldable'))
