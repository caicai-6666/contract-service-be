"""合同元数据工具：只读查询、参数定义、渲染与注册，不持有驻留缓存。"""
import asyncio
from datetime import timedelta, timezone
import logging

from pydantic import BaseModel, ConfigDict, Field

from app.infrastructure.contract_metadata_store import ContractMetadata, ContractMetadataStatus
from ..subgraph.fifo_management.schema import FIFOExecutionResult
from .registry import RegisteredTool

logger = logging.getLogger(__name__)


class GetContractMetadataArguments(BaseModel):
    """当你需要快速了解一份合同的基本信息、查看合同摘要，或确认审核人及相关日期时，使用这个工具。根据完整合同ID获取已存储的名称、摘要、审核人、签署日期和入库时间，帮助判断是否需要进一步阅读原文。摘要仅用于概览，核实具体条款时应继续查看合同文件。未记录的信息不会自动补写，不返回注意事项或关联列表。"""
    model_config = ConfigDict(extra='forbid', frozen=True)
    document_id: str = Field(pattern=r'^[0-9a-f]{64}$', description='目标合同的完整64位小写十六进制ID，必须从用户引用或工具结果中原样复制。禁止缩写、截断、使用省略号或首尾片段；不得用合同名称、文件名或关系ID替代。只有缩写时须先取得完整ID，不得猜测补全。')


def render_contract_metadata(metadata: ContractMetadata) -> str:
    """仅排版 SQLite 已存事实；签署日期不进行时区转换或日期推断。"""
    def recorded(value):
        return value if value and value.strip() else '未记录'
    time = metadata.ingested_at.astimezone(timezone(timedelta(hours=8))).isoformat(sep=' ', timespec='seconds')
    return '\n'.join([
        '# 合同基本信息', '', f'合同名称：{recorded(metadata.file_name)}',
        f'合同 ID：{metadata.document_id}', f'审核人：{recorded(metadata.reviewer)}',
        f'签署日期：{recorded(metadata.contract_time)}', f'入库时间：{time}',
        '', '## 合同摘要', '', recorded(metadata.summary), '',
        '摘要仅供概览；核实具体条款请查看合同原文。',
    ])


def build_contract_metadata_registration(metadata_store):
    async def execute(operation, arguments):
        # SQLite 同步读取放在线程中执行，避免阻塞主助手异步循环。
        try:
            metadata = await asyncio.to_thread(metadata_store.get, arguments.document_id)
            if metadata is None:
                code, message = 'contract_not_found', '合同不存在或已删除，请核对完整合同ID。'
            elif metadata.status is ContractMetadataStatus.DELETING:
                code, message = 'contract_deleting', '合同正在删除，暂时无法读取元数据。'
            elif metadata.status is not ContractMetadataStatus.READY:
                code, message = 'contract_not_ready', '合同尚未完成入库，暂时无法读取元数据。'
            else:
                return FIFOExecutionResult(status='succeeded', tool_result={
                    'status': 'success', 'document_id': metadata.document_id,
                    'content': render_contract_metadata(metadata)})
        except Exception:
            logger.exception('合同元数据读取失败')
            code, message = 'query_failed', '合同元数据读取失败，请稍后重试；不能据此判断合同不存在。'
        return FIFOExecutionResult(status='failed', tool_result={'status': 'error', 'code': code, 'message': message})
    return RegisteredTool('get_contract_metadata', GetContractMetadataArguments.__doc__,
                          GetContractMetadataArguments, execute, return_types=('ordinary',))
