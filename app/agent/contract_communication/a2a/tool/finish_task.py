"""跨平台任务的唯一交付工具，不支持中途用户通知。"""
from pydantic import Field
from app.schema.communication_workspace import WorkspaceObject, WorkspaceText
from ...agent_core.tool.output import build_output_registration


class FinishA2ATaskArguments(WorkspaceObject):
    content: WorkspaceText = Field(max_length=10000, description='向调用方一次性交付的完整任务结果、已完成范围、证据与限制；缺少必要输入时说明待补充信息并结束任务，不提交私有思考或未经核实的成功声明。')


def build_output_tools(publish):
    return [build_output_registration(name='finish_task', kind='final', arguments_model=FinishA2ATaskArguments,
        description='完成本次任务或现有信息不足以继续时使用。提交完整结果或明确待补充条件并结束；成功后不再调用工具。', publish=publish)]
