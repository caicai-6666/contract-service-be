"""根据用户备注寻找合同，复用合同结果池及折叠分页。"""
import asyncio
import logging
from pydantic import BaseModel, ConfigDict, Field

from app.infrastructure.contract_note_search import retrieve_notes
from app.service.contract_note import embed_contract_note
from .contract_question_search import empty_result, failure
from app.agent.contract_communication.agent_core.tool.file_viewer import FileViewError
from app.agent.contract_communication.agent_core.tool.progress import ToolProgress
from app.agent.contract_communication.agent_core.tool.registry import RegisteredTool
from app.agent.contract_communication.agent_core.subgraph.fifo_management.schema import FIFOExecutionResult

logger = logging.getLogger(__name__)


class SearchContractsByNotesArguments(BaseModel):
    """当你需要根据用户记录的风险提醒、处理经验、补充说明或待关注事项寻找合同时，使用此工具。调用前，将检索意图整理成一段像用户实际备注那样的文字，突出已知的观察、风险、提醒或待确认事项，不为丰富查询而补造事实。检索依据是用户为合同添加的备注，不是合同原文或已核实的事实。返回有限候选的结果集ID和数量，可将结果集ID交给其他查询工具继续限定范围；零结果不生成ID，可调整查询或结束。"""
    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=10000, description='根据检索意图拟写的一段用户备注，用自然的陈述或提醒语气表达已知观察、风险、关注事项或待确认事项，不直接照抄查找请求。例如用户要找被提醒回款有风险的合同，可写“需关注该合同的回款风险。”只有已知拖欠货款时，才可加入“客户存在拖欠货款的情况”。保留已有的具体名称、编号、条件、否定与不确定性；未知信息直接省略，不为丰富查询而补造原因、进展或事实。')
    result_id: str | None = Field(default=None, min_length=1, description='可选的本会话已有合同结果集ID，必须完整复制工具反馈。传入后只查询该集合内合同的备注；省略或null使用本次请求的初始范围，未限定时查询全部已就绪合同。不能传合同ID、空字符串或缩写；引用失效报错，不退回全库。')


def build_contract_note_search_registration(*, results, metadata_store, settings, authorize):
    async def search(operation, arguments):
        try:
            await authorize()
            scope = results.document_ids(arguments.result_id)
            if scope == ():
                return empty_result()
            # 与备注正文共用编码契约；无向量的旧备注仍能参与 BM25。
            embedding = await embed_contract_note(arguments.query, settings.embedding)
            hits, notes = await asyncio.to_thread(retrieve_notes, metadata_store.database_path,
                arguments.query, embedding, scope, top_k=settings.communication_contract_note_search_top_k)
            await authorize()
            if arguments.result_id is not None:
                results.document_ids(arguments.result_id)
            if not hits:
                return empty_result()
            result_id = results.put(hits, parent_result_id=arguments.result_id, search_type='notes', note_ids=notes)
            return FIFOExecutionResult(status='succeeded', tool_result={'status':'success',
                'result_id':result_id, 'count':len(hits),
                'message':'已根据用户备注找到候选合同，可引用结果集继续查询。备注是用户记录，不代表已核实的合同事实。'})
        except FileViewError as exc:
            return failure(exc.code,str(exc))
        except PermissionError:
            return failure('session_unavailable','会话访问权限已失效，请重新检索。')
        except Exception:
            logger.exception('合同备注检索失败')
            return failure('search_failed','备注编码或检索失败，请稍后重试；不能据此判断没有相关备注。')
    return RegisteredTool('search_contracts_by_notes', SearchContractsByNotesArguments.__doc__,
        SearchContractsByNotesArguments, search, progress=ToolProgress('local-search','正在查阅合同备注'))
