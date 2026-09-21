"""按关系描述检索边；独立会话结果池和两端合同分页渲染。"""
import asyncio
import hmac
import logging
from pydantic import BaseModel, ConfigDict, Field
from app.infrastructure.contract_metadata_store import ContractMetadataStatus
from app.service.contract_relation_embedding import embed_relation_query
from app.service.contract_relation_search import rank_relations
from .contract_image_search import ContractSearchResults
from ..subgraph.contract_retrieval.tool.contract_question_search import failure
from .file_viewer import FileViewError
from .registry import RegisteredTool
from .progress import ToolProgress
from ..subgraph.fifo_management.schema import FIFOExecutionResult

logger=logging.getLogger(__name__)

class SearchContractRelationsArguments(BaseModel):
    """当你需要根据合同之间的关联寻找目标合同时，使用此工具。将查询写成用户记录关系时的简短陈述句，描述合同如何关联、各自角色及已知条件，例如“补充协议调整原合同的付款安排”。可指定一个合同，只检索与其直接相连的边；省略则检索全部可用关系。返回的是边结果集，不是合同列表；使用view_contract_relation_search_results查看两端合同ID、名称、摘要及关系描述。关系描述来自用户，不单独证明法律效力；零结果不生成ID。"""
    model_config=ConfigDict(extra='forbid',frozen=True,str_strip_whitespace=True)
    query: str=Field(min_length=1,max_length=10000,description='根据已知线索拟写的关系备注，用简短陈述句描述关系类型、双方角色、关联原因及已知条件。例如“新合同替代旧合同”。仅知道同一项目时写“两份合同属于同一项目”，不要补造采购与施工配套等未知角色；保留否定和不确定性。')
    contract_id: str|None=Field(default=None,pattern=r'^[0-9a-f]{64}$',description='可选的起点合同完整64位小写SHA-256 ID，从上下文原样复制，不可缩写、猜测或使用结果集ID。传入后先限定该合同的一跳关系再匹配描述；省略或null查询全部可用关系。合同不可用时返回错误，不退回全库。')

class ViewContractRelationSearchResultsArguments(BaseModel):
    """当你需要根据关系检索结果定位合同或查看下一页关联时，使用此工具。每条边展示两端合同的完整ID、名称、摘要及用户关系描述；A/B只区分展示位置，不代表方向。指定起点时它固定展示为A。结果驻留本会话，驱逐后重新检索。"""
    model_config=ConfigDict(extra='forbid',frozen=True)
    result_id: str=Field(min_length=1,description='search_contract_relations返回的完整关系结果集ID，必须原样复制；不能使用合同ID、普通合同搜索结果ID或其他会话引用。')
    page: int|None=Field(default=None,strict=True,description='从1开始的页码；省略或null首次第1页、后续下一页。显式指定可重新查看，非法页或到末尾不会移动游标。')

class ContractRelationSearchResults(ContractSearchResults):
    """独立池复用签名、LRU和游标机制；只驻留边ID、分数及起点。"""
    resource_prefix='relation-search:'
    page_title='合同关系检索列表'

    def __init__(self,metadata_store,service,**kwargs):
        super().__init__(metadata_store,**kwargs)
        self.service=service

    async def search(self,args,settings):
        if self._closed:raise FileViewError('session_closed','会话已释放，请重新检索。')
        rows=await self.service.search_candidates(args.contract_id)
        if self._closed:raise FileViewError('session_closed','会话已释放，请重新检索。')
        if not rows:return FIFOExecutionResult(status='succeeded',tool_result={'status':'success','count':0,'message':'未找到匹配关系，未生成结果集ID，无需翻页。'})
        embedding=await embed_relation_query(args.query,settings.embedding)
        hits=await asyncio.to_thread(rank_relations,rows,args.query,embedding,top_k=settings.communication_relation_search_top_k)
        if self._closed:raise FileViewError('session_closed','会话已释放，请重新检索。')
        if not hits:return FIFOExecutionResult(status='succeeded',tool_result={'status':'success','count':0,'message':'未找到匹配关系，未生成结果集ID，无需翻页。'})
        rid=self.put(hits)
        self._results[rid].anchor_contract_id=args.contract_id
        return FIFOExecutionResult(status='succeeded',tool_result={'status':'success','result_id':rid,'count':len(hits),'message':'查询成功，请使用view_contract_relation_search_results查看关系与两端合同。'})

    async def resolve_page(self,reference):
        entry=self._results.get(reference.resource_id)
        if self._closed or entry is None:return [{'type':'text','text':'关系检索结果已释放或驱逐，请重新检索。'}]
        try:
            nonce,signature=reference.display_id.split('.',1);page=int(reference.locator)
            if reference.media_type!='text' or not hmac.compare_digest(signature,self._sign(reference.resource_id,reference.locator,nonce)) or not 1<=page<=(len(entry.hits)+self.page_size-1)//self.page_size:
                raise ValueError('reference')
        except (ValueError,AttributeError) as exc:raise FileViewError('invalid_reference','关系页面引用无效，请重新查看。') from exc
        selected=entry.hits[(page-1)*self.page_size:page*self.page_size]
        try:
            rows=await self.service.search_candidates(relation_ids=[rid for rid,_ in selected],limit=self.page_size)
            current={r['relation_id']:r for r in rows}
            def render():
                total_pages=(len(entry.hits)+self.page_size-1)//self.page_size
                lines=['# 合同关系检索结果',f'结果集 ID：{reference.resource_id}',f'第 {page} / {total_pages} 页 · 共 {len(entry.hits)} 条关系',
                    '按RRF综合分数降序；分数不是概率。A/B不表示方向，关系描述是用户记录，不属于合同原文。']
                for index,(rid,score) in enumerate(selected,(page-1)*self.page_size+1):
                    row=current.get(rid)
                    a=self._metadata.get(row['document_id_a']) if row else None
                    b=self._metadata.get(row['document_id_b']) if row else None
                    lines+=['',f'## 关联 {index}',f'关系 ID：{rid}']
                    if a is None or b is None or a.status is not ContractMetadataStatus.READY or b.status is not ContractMetadataStatus.READY:
                        lines+=['关系或两端合同已不可用，请重新检索。'];continue
                    aid,bid=row['document_id_a'],row['document_id_b']
                    if bid==entry.anchor_contract_id:a,b=b,a;aid,bid=bid,aid
                    lines+=[f'RRF综合分数：{score:.6g}']
                    for label,doc,meta in [('A',aid,a),('B',bid,b)]:
                        lines+=['',f'### 合同 {label}',f'合同 ID：{doc}',f'名称：{meta.file_name}','摘要：',meta.summary or '未记录']
                    lines+=['','### 关系描述',row['description']]
                lines+=['','已到最后一页。' if page==total_pages else f'可继续查看第 {page+1} 页。']
                return '\n'.join(lines)
            text=await asyncio.to_thread(render)
        except Exception:
            logger.exception('关系检索页读取失败');text='关系页面读取失败，请重试，不能据此判断关系不存在。'
        if self._closed or self._results.get(reference.resource_id) is not entry:return [{'type':'text','text':'关系结果已释放，请重新检索。'}]
        self._results.move_to_end(reference.resource_id)
        return [{'type':'text','text':text}]


def build_contract_relation_search_registrations(pool,settings):
    async def search(operation,args):
        try:return await pool.search(args,settings)
        except FileViewError as exc:return failure(exc.code,str(exc))
        except Exception:
            logger.exception('关系检索失败')
            return failure('search_failed','关系检索失败，请确认合同可用或指定合同缩小范围后重试；不能据此判断没有关系。')
    async def view(operation,args):return await pool.view(args.result_id,args.page)
    return (
        RegisteredTool('search_contract_relations',SearchContractRelationsArguments.__doc__,SearchContractRelationsArguments,search,progress=ToolProgress('local-search','正在查阅合同关系')),
        RegisteredTool('view_contract_relation_search_results',ViewContractRelationSearchResultsArguments.__doc__,ViewContractRelationSearchResultsArguments,view,return_types=('ordinary','foldable')),
    )
