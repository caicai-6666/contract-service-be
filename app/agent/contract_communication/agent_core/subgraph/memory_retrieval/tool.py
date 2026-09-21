"""检索子Agent的条件设置与最终查询工具；参数、描述和执行映射共用同一份定义。"""
from datetime import date as calendar_date, timedelta
from typing import Annotated, TYPE_CHECKING

from pydantic import Field, StringConstraints, ValidationError, model_validator

from app.infrastructure.model_json import load_model_json, validate_model_payload
from .schema import RetrievalModel
from .session import RetrievalConditions, TaskEndStatus

if TYPE_CHECKING:
    from .session import MemoryRetrievalSession

QueryText = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]


class SetTimeFilterArguments(RetrievalModel):
    """限定要查找的任务创建时间。可指定一个北京时间日期或时间范围；再次调用完整替换原时间条件，全部省略或传null可清除限制。"""
    date: str | None = Field(default=None, strict=True, pattern=r'^\d{4}-\d{2}-\d{2}$', description='单个北京时间自然日，格式YYYY-MM-DD，例如2026-09-15；覆盖当天零点至次日零点，不含次日。不能与时间范围同时设置。')
    created_from: int | None = Field(default=None, ge=0, strict=True, description='任务创建时间的UTC Unix毫秒下界，包含此时刻；null表示没有下界。使用范围时date必须为null。')
    created_before: int | None = Field(default=None, ge=0, strict=True, description='任务创建时间的UTC Unix毫秒上界，不含此时刻；null表示没有上界。有两个边界时必须大于created_from。')

    @model_validator(mode='after')
    def validate_time(self):
        if self.date is not None:
            if self.created_from is not None or self.created_before is not None:
                raise ValueError('date与时间范围互斥，请只保留一种设置方式')
            try:
                day = calendar_date.fromisoformat(self.date)
                day + timedelta(days=1)
            except (ValueError, OverflowError) as exc:
                raise ValueError('date必须是有效自然日，且可以计算次日边界') from exc
        RetrievalConditions(created_from=self.created_from, created_before=self.created_before)
        return self


class SetStatusFilterArguments(RetrievalModel):
    """限定要查找的任务终态，可选择多个。再次调用完整替换原限制；省略或传null恢复不限终态。"""
    statuses: tuple[TaskEndStatus, ...] | None = Field(default=None, description='终态列表：completed正常完成、cancelled用户终止、superseded用户调整方向、rejected门禁拒绝、failed执行失败、expired未激活过期。各项不能重复；null不限，空列表不合法。某类任务可能没有已加工的检索内容。')

    @model_validator(mode='after')
    def validate_statuses(self):
        RetrievalConditions(statuses=self.statuses)
        return self


class SetUserInputQueryArguments(RetrievalModel):
    """设置用户输入的检索文本，查找历史用户问题或随问题提供的文件。替换该字段原查询；null取消该字段，其他字段保持不变。"""
    query: QueryText | None = Field(description='查找历史问题时以用户口吻描述问题或要求；查找文件时可用file_name:文件名、display_name:展示名称、summary:摘要中的任一或多个字段。只写已知线索，不添加file_id或页数。非空文本启用该字段的BM25和向量召回，null取消。')


class SetIntermediateOutputQueryArguments(RetrievalModel):
    """设置中途输出的检索文本，查找助手曾向用户报告的阶段性发现、处理进展、待确认事项或下一步动作。替换原查询；null取消该字段。"""
    query: QueryText | None = Field(description='描述希望找回的公开阶段性发现、处理进展、待确认事项或下一步动作，可包含对象和限定条件；不检索内部思考或工具日志。非空文本同时用于该字段的BM25和向量召回，null取消。')


class SetFinalOutputQueryArguments(RetrievalModel):
    """设置最终结论的检索文本，查找助手最终答复中的结论、事实、建议或未解决事项。替换原查询；null取消该字段。"""
    query: QueryText | None = Field(description='描述希望找回的最终答复内容，保留相关对象、条件以及否定或不确定性，不预设未经确认的结论。非空文本同时用于该字段的BM25和向量召回，null取消；无最终答复的任务不会出现在该路召回中。')


class ExecuteQueryArguments(RetrievalModel):
    """使用当前已设置的条件执行查询。至少启用一个检索字段；成功包括无命中，成功即结束本次检索，不需要再次确认。"""


_TOOL_MODELS = {
    'execute_query': ExecuteQueryArguments,
    'set_time_filter': SetTimeFilterArguments,
    'set_status_filter': SetStatusFilterArguments,
    'set_user_input_query': SetUserInputQueryArguments,
    'set_intermediate_output_query': SetIntermediateOutputQueryArguments,
    'set_final_output_query': SetFinalOutputQueryArguments,
}


def build_memory_retrieval_tools() -> list[dict]:
    """仅供检索子Agent注入，不注册到主助手；查询成功由外围结束子Agent。"""
    return [{'type': 'function', 'function': {
        'name': name, 'description': model.__doc__,
        'parameters': model.model_json_schema(), 'strict': False,
    }} for name, model in _TOOL_MODELS.items()]


async def execute_memory_retrieval_tool(session: 'MemoryRetrievalSession', name: str, arguments: dict | str) -> dict:
    """执行单个已解析调用；调用数、纠错轨迹清理和私有审计由未来Agent循环管理。"""
    model = _TOOL_MODELS.get(name)
    if model is None:
        return {'status': 'error', 'error_code': 'unknown_tool',
                'message': '工具名称不合法，请使用已提供的当前提供的检索工具。'}
    try:
        # 复用统一传输兼容层，例如把字符串形式的JSON数组转换回数组；不猜测正文。
        payload = load_model_json(arguments) if isinstance(arguments, str) else arguments
        args = validate_model_payload(model, payload)
        if name == 'execute_query':
            return (await session.execute_query()).model_dump(mode='json')
        conditions = getattr(session, name)(**args.model_dump())
    except ValidationError as exc:
        details = [{'field': '.'.join(map(str, item['loc'])) or 'arguments', 'message': item['msg']}
                   for item in exc.errors(include_url=False, include_input=False, include_context=False)]
        return {'status': 'error', 'error_code': 'invalid_arguments',
                'message': '参数不合法，原查询条件保持不变；请按字段说明修正后重试。', 'details': details}
    except ValueError as exc:
        return {'status': 'error', 'error_code': 'invalid_operation',
                'message': f'{exc}；原查询条件保持不变，请修正后重试。'}
    return {'status': 'success', 'message': '查询条件已更新，尚未执行检索。',
            'conditions': conditions.model_dump(mode='json')}
