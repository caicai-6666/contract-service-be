"""输出工具的公共执行适配；交付方式由模式回调决定。"""
from ..output import UserOutput, UserOutputReceipt, UserOutputRejected
from ..subgraph.fifo_management.schema import FIFOExecutionResult
from .registry import RegisteredTool


def build_output_registration(*, name, description, arguments_model, kind, publish):
    async def execute(operation, arguments):
        try:
            receipt = UserOutputReceipt.model_validate(await publish(UserOutput(
                task_id=operation.task_id, call_id=operation.call_id, kind=kind, content=arguments.content)))
        except UserOutputRejected:
            return FIFOExecutionResult(status='failed', tool_result={
                'error': '输出被拒绝，结果未提交；请依据当前任务状态处理。'})
        return FIFOExecutionResult(status='succeeded', tool_result={
            'status': 'success', 'message_id': receipt.message_id,
            'finish_requested': kind == 'final',
            'message': '最终反馈已提交，本轮不再继续调用工具。' if kind == 'final' else '中途输出已提交，请继续执行本轮任务。',
        })
    return RegisteredTool(name, description, arguments_model, execute, output_kind=kind)
