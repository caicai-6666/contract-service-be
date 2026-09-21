"""根据合同名称寻找合同，复用合同结果池及折叠分页。"""
import asyncio
import logging
from pydantic import BaseModel, ConfigDict, Field

from app.infrastructure.contract_name_search import retrieve_names
from .contract_question_search import empty_result, failure
from app.agent.contract_communication.agent_core.tool.file_viewer import FileViewError
from app.agent.contract_communication.agent_core.tool.progress import ToolProgress
from app.agent.contract_communication.agent_core.tool.registry import RegisteredTool
from app.agent.contract_communication.agent_core.subgraph.fifo_management.schema import FIFOExecutionResult

logger = logging.getLogger(__name__)


class SearchContractsByNameArguments(BaseModel):
    """当你知道或能够根据已知线索拟写目标合同名称，希望按名称找到合同时，使用此工具。将query写成“xxx合同”式的简短名称，例如“生产设备采购合同”或“仓库租赁合同”；不写成查找请求、问题或合同摘要，不补造未知主体、项目或编号。只匹配合同名称，命中不代表条款已确认。返回有限候选的结果集ID和数量，可引用结果集继续查询；零结果不生成ID，可调整查询或结束。"""
    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=1000, description='根据已有线索拟写的“xxx合同”式简短名称，例如“生产设备采购合同”“仓库租赁合同”。保留已知的主体、项目、标的或编号等名称线索；不要写“帮我找……”式请求、问句或摘要，不猜测未知名称和信息。')
    result_id: str | None = Field(default=None, min_length=1, description='可选的本会话已有合同结果集ID，必须完整复制工具反馈。传入后只查询该集合内合同的名称；省略或null使用本次请求的初始范围，未限定时查询全部已就绪合同。不能传合同ID、空字符串或缩写；引用失效报错，不退回全库。')


def build_contract_name_search_registration(*, results, metadata_store, settings, authorize):
    async def search(operation, arguments):
        try:
            await authorize()
            scope = results.document_ids(arguments.result_id)
            if scope == ():
                return empty_result()
            hits = await asyncio.to_thread(retrieve_names, metadata_store.database_path,
                arguments.query, scope, top_k=settings.communication_contract_name_search_top_k)
            await authorize()
            if arguments.result_id is not None:
                results.document_ids(arguments.result_id)
            if not hits:
                return empty_result()
            result_id = results.put(hits, parent_result_id=arguments.result_id, search_type='name')
            return FIFOExecutionResult(status='succeeded', tool_result={'status':'success',
                'result_id':result_id, 'count':len(hits),
                'message':'已根据合同名称找到候选合同，可引用结果集继续查询。名称匹配不代表条款已确认，请查看原文核实。'})
        except FileViewError as exc:
            return failure(exc.code,str(exc))
        except PermissionError:
            return failure('session_unavailable','会话访问权限已失效，请重新检索。')
        except Exception:
            logger.exception('合同名称检索失败')
            return failure('search_failed','合同名称检索失败，请稍后重试；不能据此判断没有相关名称。')
    return RegisteredTool('search_contracts_by_name', SearchContractsByNameArguments.__doc__,
        SearchContractsByNameArguments, search, progress=ToolProgress('local-search','正在查阅合同名称'))
