"""主助手统一合同候选查询入口；不公开内部工具或中间池。"""
from pydantic import BaseModel,ConfigDict,Field
from ..subgraph.contract_retrieval import build_contract_retrieval_subgraph,ContractRetrievalRequest
from ..subgraph.fifo_management.schema import FIFOExecutionResult
from .registry import RegisteredTool
from .progress import ToolProgress
from .contract_image_search import ContractSearchResults
from .file_viewer import FileViewError

class ContractRetrievalResults(ContractSearchResults):
    """每个驻留会话独立保存最终候选；不与图片或子图中间池共享容量。"""
    render_note_evidence = False
    resource_prefix = 'contract-query:'
    page_title = '合同候选列表'

    def put_result(self, result):
        candidates = result.candidates
        if not candidates:
            raise ValueError('空结果不创建结果集ID')
        search_types = {item.search_type for item in candidates}
        if len(search_types) != 1:
            raise ValueError('最终结果必须来自同一个查询集合')
        result_id = self.put(((item.document_id, item.score) for item in candidates),
                             search_type=candidates[0].search_type)
        entry = self._results[result_id]
        if len({(item.score_kind,item.has_relevance) for item in candidates}) != 1:
            self._results.pop(result_id)
            raise ValueError('最终结果排序元数据不一致')
        entry.hits = tuple((item.document_id,item.score) for item in candidates)
        entry.raw_hits = tuple((item.document_id,item.raw_score if item.raw_score is not None else item.score) for item in candidates)
        entry.score_kind = candidates[0].score_kind
        entry.has_relevance = candidates[0].has_relevance
        entry.explanation = result.explanation
        return result_id

    async def resolve_page(self, reference):
        entry = self._results.get(reference.resource_id)
        blocks = await super().resolve_page(reference)
        if entry is not None and not self._closed and self._results.get(reference.resource_id) is entry:
            explanation = getattr(entry, 'explanation', '')
            if explanation:
                blocks.append({'type':'text','text':'查询说明：'+explanation})
        return blocks


class SearchContractsArguments(BaseModel):
    """当你需要按名称、整体内容、希望解答的问题、具体条款或用户备注寻找合同候选时，使用此工具。用自然语言说明完整查找条件，内部助手会选择查询方式并逐步限定范围，最终列表保存到本会话独立缓存池，返回结果集ID和数量，调用view_contract_candidates分页查看。零结果不生成ID。可传图片检索或此前最终候选的结果集进一步筛选。结果是相关候选，不代表所有业务条件已经核实，必要时继续查看合同原文。"""
    model_config=ConfigDict(extra='forbid',frozen=True,str_strip_whitespace=True)
    query: str=Field(min_length=1,max_length=10000,description='完整自然语言查找需求，保留已知主体、名称线索、业务条件、否定和不确定性，说明必须满足的条件；不需要自行选择内部查询方式。')
    result_id: str|None=Field(default=None,min_length=1,description='可选的当前会话图片检索结果集或最终合同候选结果集完整ID，用于限定初始合同范围；省略或null为全部ready合同。失效引用报错，不退回全库，不能传合同ID、关系结果ID或内部候选ID。')

def build_contract_retrieval_registration(*,results,final_results,metadata_store,settings,authorize,es_client=None,client=None,audit=None,field_catalog=None,category_catalog=None):
    async def execute(operation,args):
        try:
            await authorize()
            source_pool = None
            if args.result_id is not None:
                if args.result_id.startswith(final_results.resource_prefix):
                    source_pool = final_results
                elif args.result_id.startswith(results.resource_prefix):
                    source_pool = results
                else:
                    raise FileViewError('result_unavailable', '结果集类型不适用于合同范围，请使用图片或最终候选结果集ID。')
            scope=source_pool.document_ids(args.result_id) if source_pool is not None else None
            async def guarded():
                await authorize()
                if source_pool is not None:source_pool.document_ids(args.result_id)
            graph=build_contract_retrieval_subgraph(metadata_store=metadata_store,settings=settings,authorize=guarded,es_client=es_client,client=client,audit=audit,field_catalog=field_catalog,category_catalog=category_catalog)
            output=await graph.ainvoke({'request':ContractRetrievalRequest(query=args.query,initial_document_ids=scope,initial_ranking=source_pool.ranking_snapshot(args.result_id) if source_pool is not None else None)})
            result=output['result']
            await guarded()
            if result.status=='error':return FIFOExecutionResult(status='failed',tool_result={'status':'error','message':result.error})
            if not result.candidates:
                return FIFOExecutionResult(status='succeeded',tool_result={'status':'success','count':0,
                    'message':'没有找到合适的合同，未生成结果集ID，无需查看。','explanation':result.explanation})
            result_id=final_results.put_result(result)
            return FIFOExecutionResult(status='succeeded',tool_result={'status':'success','result_id':result_id,
                'count':len(result.candidates),'explanation':result.explanation,
                'message':'查询完成，请使用view_contract_candidates分页查看最终候选。'})
        except FileViewError as exc:
            return FIFOExecutionResult(status='failed',tool_result={'status':'error','code':exc.code,'message':str(exc)})
        except Exception:
            return FIFOExecutionResult(status='failed',tool_result={'status':'error','message':'合同查询范围或会话不可用，请核对引用后重试。'})
    return RegisteredTool('search_contracts',SearchContractsArguments.__doc__,SearchContractsArguments,execute,
        progress=ToolProgress('local-search','正在查找合同候选'))


class ViewContractCandidatesArguments(BaseModel):
    """当你需要查看search_contracts返回的合同候选或继续翻页时，使用此工具。每条展示完整合同ID、名称、摘要及综合排序分数；融合结果附带本轮原始分数，附带查询说明。结果驻留当前会话，驱逐或重启后需要重新查询；相关性不等于合同约定已核实。"""
    model_config=ConfigDict(extra='forbid',frozen=True)
    result_id: str=Field(min_length=1,description='search_contracts返回的完整最终候选结果集ID（contract-query:开头）。原样复制，不可使用图片、关系或子图内部结果集ID，不可缩写或引用其他会话结果。')
    page: int|None=Field(default=None,strict=True,description='从1开始的页码；省略或null首次读取第1页，后续读取下一页。显式指定可重复查看；非法页或到末尾不移动游标。')


def build_contract_candidates_view_registration(*,results,authorize):
    async def execute(operation,args):
        try:
            await authorize()
            return await results.view(args.result_id,args.page)
        except PermissionError:
            return FIFOExecutionResult(status='failed',tool_result={'status':'error','code':'session_unavailable','message':'会话访问权限已失效，请重新查询。'})
    return RegisteredTool('view_contract_candidates',ViewContractCandidatesArguments.__doc__+f'每页{results.page_size}条。',
        ViewContractCandidatesArguments,execute,return_types=('ordinary','foldable'))
