"""一键合同库统计：固定统计口径、时间参数、可读渲染和工具注册。"""
import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from ..subgraph.fifo_management.schema import FIFOExecutionResult
from .registry import RegisteredTool

logger = logging.getLogger(__name__)


class GetContractLibraryStatisticsArguments(BaseModel):
    """当你需要了解合同库有多少合同、类型与审核人分布，以及注意事项和关联覆盖情况时，使用这个工具一次获取整体统计。可按入库时间限定范围；不传时间则统计全库成功入库合同。时间不是签署日期，也不用于筛选备注或关系的创建时间。返回所选合同当前的统计，不是任意历史时刻的快照。无需先检索合同或逐份读取文件。"""
    model_config = ConfigDict(extra='forbid', frozen=True)
    start_time: AwareDatetime | None = Field(default=None, description='可选入库时间下界，包含该时刻。使用带时区的ISO 8601日期时间，例如2026-09-01T00:00:00+08:00；省略或null表示不限起始时间。与end_time同时提供时必须早于end_time。')
    end_time: AwareDatetime | None = Field(default=None, description='可选入库时间上界，不包含该时刻。使用带时区的ISO 8601日期时间，例如统计9月时填2026-10-01T00:00:00+08:00；省略或null表示不限结束时间。不是签署日期或备注、关系的创建时间。')

    @field_validator('start_time', 'end_time', mode='before')
    @classmethod
    def reject_numeric_time(cls, value):
        if value is not None and not isinstance(value, (str, datetime)):
            raise ValueError('时间必须为带时区的ISO 8601日期时间，不能使用时间戳')
        if isinstance(value, str) and not re.match(r'^\d{4}-\d{2}-\d{2}[Tt ]', value):
            raise ValueError('请输入完整的带时区日期时间，不能使用纯日期或数字字符串')
        return value

    @model_validator(mode='after')
    def ordered(self):
        if self.start_time is not None and self.end_time is not None and self.start_time >= self.end_time:
            raise ValueError('start_time必须早于end_time，时间范围为左闭右开')
        return self


def render_contract_statistics(data):
    def cell(value):
        return str(value).replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')
    start, end = data['start_time'], data['end_time']
    scope = '全部已入库合同' if start is None and end is None else f"{start + '（含）' if start else '不限'} 至 {end + '（不含）' if end else '不限'}"
    lines = ['# 合同库统计', '', f"统计时点：{data['as_of']}",
        f"入库时间范围：{scope}",
        f"成功入库合同：{data['total']} 份", '', '## 类型分布', '', '| 类型 | 数量 | 占比 |', '| --- | ---: | ---: |']
    lines.extend(f"| {cell(r['name'])} | {r['count']} | {r['percentage']:.2f}% |" for r in data['categories'])
    if not data['categories']:
        lines.append('无类别统计记录。')
    lines.extend(['', f"未归类合同：{data['uncategorized_count']} 份", '',
        '## 审核人分布', '', '| 审核人 | 合同数量 |', '| --- | ---: |'])
    lines.extend(f"| {cell(r['name'])} | {r['count']} |" for r in data['reviewers'])
    if not data['reviewers']:
        lines.append('无审核人统计记录。')
    lines.extend(['', '## 注意事项', f"注意事项总数：{data['notes']['total']} 条",
                  f"有注意事项的合同：{data['notes']['contracts_with_notes']} 份", '', '## 合同关联'])
    relations = data['relations']
    if relations is None:
        lines.append('关系统计：不可用')
    else:
        lines.extend([f"关联边：{relations['total']} 条", f"有关系的合同：{relations['contracts_with_relations']} 份",
                      f"无关系的合同：{relations['contracts_without_relations']} 份"])
    return '\n'.join(lines)


def build_contract_statistics_registration(metadata_store, relation_service):
    async def execute(operation, arguments):
        as_of = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')
        try:
            data = await asyncio.to_thread(metadata_store.library_statistics, arguments.start_time, arguments.end_time)
        except Exception:
            logger.exception('合同库统计读取失败')
            return FIFOExecutionResult(status='failed', tool_result={'status': 'error',
                'code': 'statistics_unavailable', 'message': '合同库统计暂时不可用。'})
        selected, ready = data.pop('selected_ids'), data.pop('ready_ids')
        relations = None
        if not selected:
            relations = dict(total=0, contracts_with_relations=0, contracts_without_relations=0)
        elif relation_service is not None:
            try:
                relations = await relation_service.statistics(selected, ready)
            except Exception:
                logger.exception('合同库关系统计读取失败')
        data.update(as_of=as_of, start_time=arguments.start_time.isoformat() if arguments.start_time else None,
                    end_time=arguments.end_time.isoformat() if arguments.end_time else None, relations=relations)
        return FIFOExecutionResult(status='succeeded', tool_result={
            'status': 'success' if relations is not None else 'partial',
            'content': render_contract_statistics(data)})
    return RegisteredTool('get_contract_library_statistics', GetContractLibraryStatisticsArguments.__doc__,
                          GetContractLibraryStatisticsArguments, execute, return_types=('ordinary',))
