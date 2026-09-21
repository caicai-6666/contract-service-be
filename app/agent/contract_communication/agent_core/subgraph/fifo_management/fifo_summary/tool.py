"""主题规划两工具；提取按标识完整替换，允许选择性保留及空主题。"""
from typing import Annotated
from pydantic import Field, StringConstraints
from app.schema.communication_workspace import WorkspaceObject
from .schema import SummaryTopic


class FinishPlanningArguments(WorkspaceObject):
    summary: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=1000)] = Field(
        description='简短说明筛选完成情况，最多1000字符；空主题时说明无需保留的原因，不代替主题集合或生成业务摘要。')


def build_topic_planning_tools():
    specs = [
        ('extract_topic', '选择对后续继续任务有价值的记忆主题和必要来源，在scope中给出简要提取指导；合并相关问题，不为常规历史过程单独建主题；新topic_id追加，已有topic_id完整替换并保留顺序，最多32个主题。', SummaryTopic),
        ('finish_topic_planning', '确认筛选结束，允许空主题，不强制覆盖全部来源；未引用旧主题在整次摘要成功替换后不再继承。', FinishPlanningArguments),
    ]
    return [{'type': 'function', 'function': {'name': name, 'description': description,
        'parameters': model.model_json_schema(), 'strict': False}} for name, description, model in specs]
