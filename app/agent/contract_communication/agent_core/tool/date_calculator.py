"""无状态日历运算：年月按目标月末截断，随后叠加日时分秒。"""
import calendar
from datetime import datetime, timedelta, timezone
import re

from pydantic import Field, StrictInt, field_validator
from app.schema.communication_workspace import WorkspaceObject
from ..subgraph.fifo_management.schema import FIFOExecutionResult


def parse_start_time(value: str) -> datetime:
    # 限制为可读日历日期，不接受ISO周日期、时间戳或平台相关宽松解析。
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?)?', value):
        raise ValueError('开始时间须为YYYY-MM-DD或ISO日期时间（包含时分秒）')
    offset = re.search(r'([+-])(\d{2}):(\d{2})$', value)
    if offset and (int(offset[2]) > 23 or int(offset[3]) > 59):
        raise ValueError('时区偏移无效')
    # fromisoformat会校验真实日历日期，例如2月31日必须拒绝，不能偷偷修正输入。
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone(timedelta(hours=8)))


class DateCalculateArguments(WorkspaceObject):
    """当你需要计算精确的日期值时，使用这个工具。例如，将“昨天”“三个月后”换算为具体日期，或计算已知日期前后的一段时间。工具会正确处理月末、闰年和跨年，避免手动推算错误。

    提供有依据的开始时间，以及需要增减的年、月、日、时、分、秒；负数表示向前计算，省略的增减量按零处理。目标月份没有对应日期时取月末，未指定时区时采用北京时间。

    本工具只进行日历运算，不计算工作日或节假日，也不判断业务期限的起止规则。
    """

    start_time: str = Field(description='计算基准，须取自当前任务时间、用户明确输入或已知事实，不得猜测。格式为YYYY-MM-DD或YYYY-MM-DDTHH:MM:SS，可附小数秒及Z或±HH:MM时区。仅日期按00:00:00处理，未指定时区按北京时间UTC+08:00；必须是有效日期。')
    years: StrictInt | None = Field(default=None, description='增加的年数，负数为减少，省略或null为0；与月份偏移合并计算后再处理月末。')
    months: StrictInt | None = Field(default=None, description='增加的月数，负数为减少，可跨年，省略或null为0；目标月没有原日期时取该月最后一天。')
    days: StrictInt | None = Field(default=None, description='年月调整完成后增加的自然日数，负数为减少，省略或null为0；不是工作日。')
    hours: StrictInt | None = Field(default=None, description='年月调整后增加的小时数，负数为减少，省略或null为0；自动跨日。')
    minutes: StrictInt | None = Field(default=None, description='年月调整后增加的分钟数，负数为减少，省略或null为0；自动进退位。')
    seconds: StrictInt | None = Field(default=None, description='年月调整后增加的秒数，负数为减少，省略或null为0；自动进退位。')

    @field_validator('start_time')
    @classmethod
    def valid_start(cls, value):
        parse_start_time(value)
        return value


def calculate_date(arguments: DateCalculateArguments) -> dict:
    start = parse_start_time(arguments.start_time)
    month_index = (start.year - 1) * 12 + start.month - 1 + (arguments.years or 0) * 12 + (arguments.months or 0)
    if not 0 <= month_index < 9999 * 12:
        raise ValueError('年月运算结果超出支持范围（公元1至9999年）')
    year, month = month_index // 12 + 1, month_index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    adjusted = start.replace(year=year, month=month, day=day)
    result = adjusted + timedelta(days=arguments.days or 0, hours=arguments.hours or 0,
                                  minutes=arguments.minutes or 0, seconds=arguments.seconds or 0)
    return {'start_time': start.isoformat(), 'result_time': result.isoformat(),
            'weekday': ('星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日')[result.weekday()],
            'month_end_clamped': day != start.day}


async def execute_date_calculation(operation, arguments):
    try:
        result = calculate_date(arguments)
    except (ValueError, OverflowError):
        return FIFOExecutionResult(status='failed', tool_result={
            'error': '日期运算超出支持范围（公元1至9999年），请缩小增减量；未生成结果。'})
    return FIFOExecutionResult(status='succeeded', tool_result=result)


def build_date_calculation_registration():
    from .registry import RegisteredTool
    return RegisteredTool('calculate_date', DateCalculateArguments.__doc__,
                          DateCalculateArguments, execute_date_calculation)
