"""条款标题与正文 BM25 查询，最高分条款决定合同排名。"""
import asyncio
import logging
import math
import re

from pydantic import BaseModel, ConfigDict, Field

from app.infrastructure.contract_metadata_store import ContractMetadataStatus
from app.agent.contract_communication.agent_core.subgraph.fifo_management.schema import FIFOExecutionResult
from app.agent.contract_communication.agent_core.tool.file_viewer import FileViewError
from app.agent.contract_communication.agent_core.tool.registry import RegisteredTool
from app.agent.contract_communication.agent_core.tool.progress import ToolProgress
from .contract_question_search import empty_result, failure

logger = logging.getLogger(__name__)


class SearchContractsByClauseArguments(BaseModel):
    """当你知道要查找的具体合同约定或条款表述时，使用这个工具。例如寻找验收不合格时暂停付款、逾期交付责任或保密义务等约定。查询会匹配合同中所有已入库条款的标题和正文，以最相关的一条条款决定合同排名；适合按具体用语和约定内容查找。命中仅表示文字相关，不能据此确认权利义务、否定或例外条件已经满足。成功返回候选结果集ID与数量，可继续限定查询；没有结果时不生成ID。"""
    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=4000, description='将查找意图改写为希望找到的简洁条款表述或关键措辞，例如“验收不合格时，买方有权要求整改并暂停支付相应款项”。保留原需求中明确的主体角色、触发条件、行为、否定和例外，不添加未知期限、金额、比例或权利。不要写查找命令或堆砌无关条款；拟写内容是检索目标，不是已确认的合同事实。')
    result_id: str | None = Field(default=None, min_length=1, description='可选的本次查询已成功返回的完整候选结果集ID。传入后仅查询该集合的合同；省略或null使用本次请求的初始范围，未限定时为全库。失效引用返回错误，不退回全库。不得输入合同ID、其他类型引用、空字符串或缩写。')


def build_clause_query(query, scope):
    """同一条款内合并标题与正文得分，再取最高条款分；不累加合同条款数。"""
    nested = {'nested': {'path': 'clauses', 'score_mode': 'max', 'query': {
        'multi_match': {'query': query, 'fields': ['clauses.title', 'clauses.content'],
                        'type': 'most_fields'}
    }}}
    if scope is None:
        return nested
    return {'bool': {'filter': [{'ids': {'values': list(scope)}}], 'must': [nested]}}


def build_contract_clause_search_registration(*, results, client, index_name, metadata_store, settings, authorize):
    async def search(operation, arguments):
        try:
            await authorize()
            scope = results.document_ids(arguments.result_id)
            if scope == ():
                return empty_result()
            top_k = settings.communication_contract_clause_search_top_k
            # 多取少量候选以过滤已删除/尚未就绪的跨库记录；计数不代表全库命中总数。
            response = await client.search(
                index=index_name, query=build_clause_query(arguments.query, scope),
                size=min(top_k * 4, 200), source=['document_id'],
                sort=[{'_score': 'desc'}, {'document_id': 'asc'}],
            )
            if response.get('timed_out') or response.get('_shards', {}).get('failed', 0):
                raise RuntimeError('ES 条款查询未完整成功')

            def collect():
                hits, seen = [], set()
                allowed = set(scope) if scope is not None else None
                for hit in response['hits']['hits']:
                    doc = hit['_source']['document_id']
                    raw_score = hit['_score']
                    if isinstance(raw_score, bool) or not isinstance(raw_score, (float, int)):
                        raise ValueError('非法条款分数')
                    score = float(raw_score)
                    if not isinstance(doc, str) or not re.fullmatch(r'[0-9a-f]{64}', doc) or not math.isfinite(score) or score < 0:
                        raise ValueError('非法合同或条款分数')
                    if doc in seen or (allowed is not None and doc not in allowed):
                        continue
                    seen.add(doc)
                    metadata = metadata_store.get(doc)
                    if metadata is not None and metadata.status is ContractMetadataStatus.READY:
                        hits.append((doc, score))
                return sorted(hits, key=lambda item: (-item[1], item[0]))[:top_k]

            hits = await asyncio.to_thread(collect)
            await authorize()
            if arguments.result_id is not None:
                results.document_ids(arguments.result_id)
            if not hits:
                return empty_result()
            result_id = results.put(hits, parent_result_id=arguments.result_id, search_type='clause')
            return FIFOExecutionResult(status='succeeded', tool_result={
                'status': 'success', 'result_id': result_id, 'count': len(hits),
                'message': '条款查询成功，返回有限合同候选；文字命中不等于已核实约定，可引用结果集继续查询。',
            })
        except FileViewError as exc:
            return failure(exc.code, str(exc))
        except PermissionError:
            return failure('session_unavailable', '会话访问权限已失效，请重新发起检索。')
        except Exception:
            logger.exception('合同条款检索失败')
            return failure('search_failed', '合同条款检索失败，请稍后重试；不能据此判断没有相关合同。')

    return RegisteredTool('search_contracts_by_clause', SearchContractsByClauseArguments.__doc__,
        SearchContractsByClauseArguments, search, progress=ToolProgress('local-search', '正在查阅相关合同'))
