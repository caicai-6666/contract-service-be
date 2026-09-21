"""候选结果两表OR合并，不访问数据库、不向模型展开名单。"""
from pydantic import Field
from ..schema import RetrievalModel
from app.agent.contract_communication.agent_core.tool.registry import RegisteredTool
from app.agent.contract_communication.agent_core.tool.file_viewer import FileViewError
from app.agent.contract_communication.agent_core.subgraph.fifo_management.schema import FIFOExecutionResult
from .contract_question_search import failure


class UnionContractResultsArguments(RetrievalModel):
    """当需求允许满足两组条件中的任意一组时，先分别查询，再用此工具合并两个结果集。按合同ID去重保留并集，两表有排名时等权RRF；未出现的一路不贡献分数，纯筛选不贡献排名。仅纯筛选命中的合同仍保留。不要为了抬高分数重复合并同一批证据。成功仅返回新结果集ID与数量，可继续筛选或提交结束。"""
    left_result_id: str = Field(min_length=1, description='本次子智能体成功查询返回的第一张完整candidate-set结果集ID，不接受外部引用或合同ID；两表顺序不影响融合。')
    right_result_id: str = Field(min_length=1, description='本次子智能体成功查询返回的第二张完整candidate-set结果集ID。失效引用报错；相同ID直接复用原集合。零结果没有ID，此时直接使用另一张非空表，无需合并。')


def build_union_contract_results_registration(*, pool, authorize):
    async def execute(operation, args):
        try:
            await authorize()
            rid = pool.union(args.left_result_id, args.right_result_id)
            return FIFOExecutionResult(status='succeeded', tool_result={'status':'success',
                'result_id':rid, 'count':len(pool.document_ids(rid)),
                'message':'已合并两表并集并去重；仅对有相关性排名的来源进行RRF融合，未命中一路不贡献分数。'})
        except FileViewError as exc:
            return failure(exc.code, str(exc))
        except PermissionError:
            return failure('session_unavailable', '会话权限已失效，请重新检索。')
        except ValueError as exc:
            return failure('invalid_union', str(exc))
    return RegisteredTool('union_contract_results', UnionContractResultsArguments.__doc__,
                          UnionContractResultsArguments, execute)
