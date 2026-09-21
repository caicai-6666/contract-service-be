"""按问题融合向量查询合同；复用当前会话的结果池与分页工具。"""
import asyncio
import logging
import math
import re

from pydantic import BaseModel, ConfigDict, Field

from app.agent.contract_extraction.subgraph.retrieval_view_generation.question_generation.prompt.embedding import render_user_query_embedding_input
from app.infrastructure.embedding import EmbeddingClient, EmbeddingRequestError
from app.infrastructure.contract_metadata_store import ContractMetadataStatus
from app.agent.contract_communication.agent_core.subgraph.fifo_management.schema import FIFOExecutionResult
from app.agent.contract_communication.agent_core.tool.file_viewer import FileViewError
from app.agent.contract_communication.agent_core.tool.registry import RegisteredTool
from app.agent.contract_communication.agent_core.tool.progress import ToolProgress

logger = logging.getLogger(__name__)


class SearchContractsByQuestionArguments(BaseModel):
    """当你需要寻找可能回答某个业务问题的合同时，使用这个工具。每份合同在入库时，会根据其内容整理出一组该合同能够回答的问题；本工具依据这些问题寻找与你当前提问相关的合同。适合按想了解什么、希望核实什么发起查询，例如“交付延误时如何承担责任？”或“验收通过后何时支付尾款？”。命中表示该合同可能提供相关信息，不代表已经确认具体约定；需要继续查看合同内容后再作判断。有结果时返回结果集ID与数量，可引用结果集继续查询；无结果不生成ID，可调整查询或结束。"""
    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=True)
    query: str = Field(min_length=1, description='将检索意图改写为用户直接向合同提问的自然问句，说明希望了解或核实什么，不直接照抄查找请求。例如“找涉及延期交付责任的合同”可改写为“交付延误时，需要承担什么责任？”。保留已知主体、交易事项、适用条件和否定限制；不预设答案，不补造未知主体、事实或约定。')
    result_id: str | None = Field(default=None, min_length=1, description='可选的已有合同检索结果集ID，必须从本会话成功反馈中完整复制。传入后只在该集合的合同中查找；省略或null使用本次请求的初始范围，未限定时为全库。失效引用返回错误，不退回全库。不能传合同ID、记忆查询ID、空字符串或缩写。')


async def encode_contract_question(query, settings):
    """查询侧编码与入库问题侧配对；只编码用户问题一次。"""
    async with EmbeddingClient(settings) as client:
        response = await client.create_embeddings(inputs=[render_user_query_embedding_input(query)])
    if response.model != settings.model or len(response.vectors) != 1:
        raise EmbeddingRequestError('查询向量响应模型或数量不一致')
    vector = response.vectors[0]
    if len(vector) != settings.dimensions or any(
        isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in vector
    ):
        raise EmbeddingRequestError('查询向量维度或数值无效')
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or norm == 0:
        raise EmbeddingRequestError('查询向量范数无效')
    return tuple(float(x/norm) for x in vector)


def build_contract_question_search_registration(*, results, client, index_name, metadata_store, settings, authorize):
    async def search(operation, arguments):
        try:
            await authorize()
            scope = results.document_ids(arguments.result_id)
            # 即使后续扩展允许空集合，也不能把显式空范围降级为全库。
            if scope == ():
                return empty_result()
            vector = await encode_contract_question(arguments.query, settings.embedding)
            await authorize()
            top_k = settings.communication_contract_question_search_top_k
            limit = min(top_k * 4, 200)
            filters = [{'exists':{'field':'vectors.question_fusion'}}]
            if scope is not None:
                filters.append({'ids':{'values':list(scope)}})
            knn = {'field':'vectors.question_fusion', 'query_vector':list(vector), 'k':limit,
                   'num_candidates':max(100, limit*2), 'filter':{'bool':{'filter':filters}}}
            threshold = settings.communication_contract_question_search_minimum_similarity
            if threshold is not None:
                knn['similarity'] = threshold
            response = await client.search(index=index_name, knn=knn, size=limit, source=['document_id'])
            if response.get('timed_out') or response.get('_shards', {}).get('failed', 0):
                raise RuntimeError('ES 查询未完整成功')
            def collect():
                hits, seen = [], set()
                allowed = set(scope) if scope is not None else None
                for hit in response['hits']['hits']:
                    doc = hit['_source']['document_id']
                    score = float(hit['_score'])*2-1
                    if not re.fullmatch(r'[0-9a-f]{64}', doc) or not math.isfinite(score):
                        raise ValueError('ES 返回非法合同或分数')
                    if doc in seen or (allowed is not None and doc not in allowed):
                        continue
                    seen.add(doc)
                    metadata = metadata_store.get(doc)
                    if metadata is not None and metadata.status is ContractMetadataStatus.READY:
                        hits.append((doc, max(-1.0, min(1.0, score))))
                return sorted(hits, key=lambda item:(-item[1], item[0]))[:top_k]
            hits = await asyncio.to_thread(collect)
            await authorize()
            if arguments.result_id is not None:
                # 查询期间旧引用被驱逐时，不发布基于失效引用的派生结果。
                results.document_ids(arguments.result_id)
            if not hits:
                return empty_result()
            result_id = results.put(hits, parent_result_id=arguments.result_id, search_type='question')
            return FIFOExecutionResult(status='succeeded', tool_result={'status':'success', 'result_id':result_id,
                'count':len(hits), 'message':'查询成功，返回可能回答该问题的有限候选；可引用结果集继续查询。'})
        except FileViewError as exc:
            return failure(exc.code, str(exc))
        except PermissionError:
            return failure('session_unavailable', '会话访问权限已失效，请重新发起检索。')
        except Exception:
            logger.exception('合同问题检索失败')
            return failure('search_failed', '问题编码或合同检索失败，请稍后重试；不能据此判断没有相关合同。')

    return RegisteredTool('search_contracts_by_question', SearchContractsByQuestionArguments.__doc__,
        SearchContractsByQuestionArguments, search, progress=ToolProgress('local-search', '正在查阅相关合同'))


def empty_result():
    return FIFOExecutionResult(status='succeeded', tool_result={'status':'success', 'count':0,
        'message':'本次检索未找到相关合同，未生成结果集ID，无需创建结果集。'})


def failure(code, message):
    return FIFOExecutionResult(status='failed', tool_result={'status':'error', 'code':code, 'message':message})
