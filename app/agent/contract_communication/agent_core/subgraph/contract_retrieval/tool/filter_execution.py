"""结构化筛选共用执行器：完整分页、范围校验、ready核验及保序入池。"""
import asyncio
import logging
import re
from app.infrastructure.contract_metadata_store import ContractMetadataStatus
from app.agent.contract_communication.agent_core.tool.file_viewer import FileViewError
from app.agent.contract_communication.agent_core.subgraph.fifo_management.schema import FIFOExecutionResult
from .contract_question_search import empty_result, failure

logger = logging.getLogger(__name__)


def build_filter_handler(*, results, client, index_name, metadata_store, authorize, build_query, success_message):
    async def search(operation, arguments):
        pit_id = None
        try:
            await authorize()
            scope = results.document_ids(arguments.result_id)
            if scope == ():
                return empty_result()
            query = build_query(arguments, scope)
            # PIT 固定本次索引视图，完整分页后才提交，避免 Top-K 截断丢掉父集合候选。
            pit = await client.open_point_in_time(index=index_name, keep_alive='1m', allow_partial_search_results=False)
            pit_id = pit['id']
            documents, seen, cursor = [], set(), None
            allowed = set(scope) if scope is not None else None
            for _ in range(1000):
                await authorize()
                results.document_ids(arguments.result_id)
                response = await client.search(pit={'id': pit_id, 'keep_alive': '1m'}, query=query,
                    size=500, sort=[{'_shard_doc': 'asc'}], search_after=cursor,
                    source=['document_id'], track_total_hits=False, allow_partial_search_results=False)
                pit_id = response.get('pit_id', pit_id)
                if response.get('timed_out') or response.get('_shards', {}).get('failed', 0):
                    raise ValueError('ES 查询未完整成功')
                hits = response['hits']['hits']
                if not hits:
                    break
                for hit in hits:
                    doc = hit['_source']['document_id']
                    if not isinstance(doc, str) or not re.fullmatch(r'[0-9a-f]{64}', doc):
                        raise ValueError('ES 返回非法合同ID')
                    if allowed is not None and doc not in allowed:
                        raise ValueError('ES 返回越界合同')
                    if doc in seen:
                        raise ValueError('ES 分页返回重复合同')
                    seen.add(doc)
                    documents.append(doc)
                next_cursor = hits[-1]['sort']
                if not next_cursor or next_cursor == cursor:
                    raise ValueError('ES 分页游标未推进')
                cursor = next_cursor
            else:
                return failure('query_too_broad', '筛选范围过大，请补充条件后重试；未发布不完整结果。')
            def ready_documents():
                return [doc for doc in documents if (row := metadata_store.get(doc)) is not None
                        and row.status is ContractMetadataStatus.READY]
            documents = await asyncio.to_thread(ready_documents)
            await authorize()
            results.document_ids(arguments.result_id)
            if not documents:
                return empty_result()
            rid = results.filter(documents, parent_result_id=arguments.result_id)
            return FIFOExecutionResult(status='succeeded', tool_result={'status':'success',
                'result_id':rid, 'count':len(documents),
                'message':success_message})
        except FileViewError as exc:
            return failure(exc.code, str(exc))
        except PermissionError:
            return failure('session_unavailable', '会话权限已失效，请重新检索。')
        except Exception:
            logger.exception('合同结构化筛选失败')
            return failure('search_failed', '结构化筛选未完整完成，请重试；不能据此判断没有合同。')
        finally:
            if pit_id is not None:
                try:
                    await client.close_point_in_time(id=pit_id)
                except Exception:
                    logger.warning('释放合同筛选PIT失败，将等待到期', exc_info=True)

    return search
