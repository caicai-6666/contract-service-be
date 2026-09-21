"""根据合同摘要寻找合同，复用合同结果池及折叠分页。"""
import asyncio
import logging
from pydantic import BaseModel, ConfigDict, Field

from app.infrastructure.contract_summary_search import retrieve_summaries
from app.service.contract_summary_embedding import embed_contract_summary
from .contract_question_search import empty_result, failure
from app.agent.contract_communication.agent_core.tool.file_viewer import FileViewError
from app.agent.contract_communication.agent_core.tool.progress import ToolProgress
from app.agent.contract_communication.agent_core.tool.registry import RegisteredTool
from app.agent.contract_communication.agent_core.subgraph.fifo_management.schema import FIFOExecutionResult

logger = logging.getLogger(__name__)


class SearchContractsBySummaryArguments(BaseModel):
    """当你已经知道目标合同大概关于什么、涉及哪些具体内容，希望据此找到合同时，使用此工具。例如已知合同涉及生产设备采购、验收付款和质保安排。调用前，将已有线索整理成一段简短、稍正式的拟写合同摘要，以接近合同摘要的表达方式进行查找；这只是检索描述，不是已核实的合同摘要。只使用已知线索，不补造主体、金额、日期、期限或条款。若仅想确认某个业务问题由哪些合同回答，可使用search_contracts_by_question。摘要可能省略细节，命中后仍需查看原文核实。返回有限候选的结果集ID与数量，可引用结果集继续查询；零结果不生成ID，可调整查询或结束。"""
    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=10000, description='根据已知线索拟写的一段简短、正式的目标合同摘要，用陈述句描述合同主题、交易标的、用途及已知关键约定，不直接照抄“帮我找……”式请求，也不写成待解答的问题。例如已知涉及设备采购、验收后付尾款和质保，可写“本合同涉及生产设备采购，约定设备验收、验收后尾款支付及质量保证安排。”仅整理已知事实，未知内容直接省略，不猜测主体名称、金额、日期、期限、责任或条款。')
    result_id: str | None = Field(default=None, min_length=1, description='可选的本会话已有合同结果集ID，必须完整复制工具反馈。传入后只查询该集合内合同的摘要；省略或null使用本次请求的初始范围，未限定时查询全部已就绪合同。不能传合同ID、空字符串或缩写；引用失效报错，不退回全库。')


def build_contract_summary_search_registration(*, results, metadata_store, settings, authorize):
    async def search(operation, arguments):
        try:
            await authorize()
            scope = results.document_ids(arguments.result_id)
            if scope == ():
                return empty_result()
            # 与摘要正文共用编码契约；无向量的旧摘要仍能参与 BM25。
            embedding = await embed_contract_summary(arguments.query, settings.embedding)
            hits = await asyncio.to_thread(retrieve_summaries, metadata_store.database_path,
                arguments.query, embedding, scope, top_k=settings.communication_contract_summary_search_top_k)
            await authorize()
            if arguments.result_id is not None:
                results.document_ids(arguments.result_id)
            if not hits:
                return empty_result()
            result_id = results.put(hits, parent_result_id=arguments.result_id, search_type='summary')
            return FIFOExecutionResult(status='succeeded', tool_result={'status':'success',
                'result_id':result_id, 'count':len(hits),
                'message':'已根据合同摘要找到候选合同，可引用结果集继续查询。摘要可能省略细节，请查看原文核实约定。'})
        except FileViewError as exc:
            return failure(exc.code,str(exc))
        except PermissionError:
            return failure('session_unavailable','会话访问权限已失效，请重新检索。')
        except Exception:
            logger.exception('合同摘要检索失败')
            return failure('search_failed','摘要编码或检索失败，请稍后重试；不能据此判断没有相关摘要。')
    return RegisteredTool('search_contracts_by_summary', SearchContractsBySummaryArguments.__doc__,
        SearchContractsBySummaryArguments, search, progress=ToolProgress('local-search','正在查阅合同摘要'))
