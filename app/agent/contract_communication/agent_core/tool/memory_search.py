"""主助手发起记忆检索；会话与时间由宿主绑定，不接受模型覆盖。"""
from typing import Annotated
from pydantic import Field, StringConstraints
from ..subgraph.memory_retrieval.schema import RetrievalModel, MemoryRetrievalRequest
from ..subgraph.memory_retrieval.workflow import build_memory_retrieval_subgraph
from ..subgraph.fifo_management.schema import FIFOExecutionResult
from .registry import RegisteredTool
from .progress import ToolProgress


class SearchMemoryArguments(RetrievalModel):
    """当你需要找回当前会话中曾经讨论的问题、涉及的文件、处理进展或结论时，使用这个工具。通过检索历史任务补充当前上下文中缺失的信息，避免凭印象猜测。描述希望找回的问题、文件、阶段性反馈或最终结论，可包含明确时间和结束状态。有结果时返回query_id，再用view_memory_query查看；零结果仅返回提示，不生成ID，无需翻页。"""
    query: Annotated[str,StringConstraints(strict=True,strip_whitespace=True,min_length=1)] = Field(
        description='自然语言检索需求，说明希望找回什么历史内容；保留已知对象、文件线索、日期和任务终态，不猜测未知历史结论。无需手工选择字段或编写SQL。')


def build_memory_search_registration(*, pool, reference_time, audit=None, **agent_options):
    graph=build_memory_retrieval_subgraph(result_pool=pool,audit=audit,**agent_options)
    async def execute(operation, arguments):
        request=MemoryRetrievalRequest(conversation_id=pool.conversation_id,reference_time=reference_time,query=arguments.query)
        output=await graph.ainvoke({'request':request})
        result=output['result']
        if result.status=='error':
            return FIFOExecutionResult(status='failed',tool_result={'status':'error','code':result.error_code,'error':result.error})
        if result.total == 0:
            return FIFOExecutionResult(status='succeeded', tool_result={'status':'success', 'total':0,
                'total_pages':0, 'message':'本次查询未召回任务，未生成查询ID，无需调用翻页工具。'})
        return FIFOExecutionResult(status='succeeded',tool_result={'status':'success','query_id':result.query_id,
            'total':result.total,'total_pages':result.total_pages,
            'message':'查询已完成，请调用view_memory_query查看第一页。' if result.total else '本次查询未召回任务。'})
    return RegisteredTool('search_memory',SearchMemoryArguments.__doc__,SearchMemoryArguments,execute,
                          progress=ToolProgress(type='local-search',message='正在检索历史记忆'))
