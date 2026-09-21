"""候选查询结束工具：参数、执行适配与注册集中定义。"""
from collections.abc import Awaitable, Callable
from pydantic import Field

from app.agent.contract_communication.agent_core.tool.registry import RegisteredTool
from app.agent.contract_communication.agent_core.subgraph.fifo_management.schema import FIFOExecutionResult
from ..schema import RetrievalModel, ContractRetrievalResult
from ..session import CandidatePool

class FinishContractRetrievalArguments(RetrievalModel):
    """当你已完成必要的查询并选定最终候选集合时调用。只提交本次查询获得的引用，程序读取真实列表，不能自行生成合同。候选尚未核实的条件应明确说明。"""
    result_id: str|None=Field(default=None,description='本次查询成功返回的完整内部结果集ID。省略或null表示没有找到合适的合同并结束查询，即使池中已有候选也可这样结束。传入时必须为有效内部引用，不能提交外部结果集或编造引用。')
    explanation: str=Field(default='',max_length=2000,description='可选说明，简述采用了哪些查询、仍未验证哪些条件；未查看合同内容，不能声称所有条件已核实。')


def build_finish_contract_retrieval_registration(
    *, pool: CandidatePool, authorize: Callable[[], Awaitable[None]],
    on_complete: Callable[[ContractRetrievalResult], None],
) -> RegisteredTool:
    """只有结果回读和最终权限检查都成功后，才向循环提交结束状态。"""
    async def execute(operation, args):
        try:
            await authorize()
            value = await pool.finish(args)
            await authorize()
            on_complete(value)
            return FIFOExecutionResult(status='succeeded', tool_result={'status':'success'})
        except (ValueError, RuntimeError) as exc:
            return FIFOExecutionResult(status='failed', tool_result={'status':'error','message':str(exc)})

    return RegisteredTool('finish_contract_retrieval', FinishContractRetrievalArguments.__doc__,
                          FinishContractRetrievalArguments, execute)
