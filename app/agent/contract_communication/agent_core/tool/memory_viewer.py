"""主助手历史查询翻页工具；仅保存签名密钥，页面内容实时读取。"""
import hashlib
import hmac
import secrets
from uuid import uuid4

from pydantic import Field
from ..subgraph.memory_retrieval.schema import RetrievalModel
from ..subgraph.fifo_management.schema import FIFOExecutionResult
from app.schema.agent_tool_content import FoldableToolContent, ToolPageReference
from .registry import RegisteredTool


class ViewMemoryQueryArguments(RetrievalModel):
    """当你需要阅读记忆检索命中的历史任务、核对当时的具体内容，或继续翻阅查询结果时，使用这个工具。使用查询返回的query_id，省略页码查看下一页；首次从第1页开始，每页3条，不重新检索。"""
    query_id: str = Field(min_length=1, description='查询成功返回的完整query_id，只能查看当前会话仍驻留的查询。标识失效时需要重新检索。')
    page: int | None = Field(default=None, strict=True, description='从1开始的页码；省略或null查看下一页，首次为第1页。末页不循环；可以指定已看过的页重新查看。')


class MemoryQueryViewer:
    def __init__(self, pool):
        self.pool = pool
        self._key = secrets.token_bytes(32)

    def _signature(self, resource, locator, nonce):
        return hmac.new(self._key, f'{resource}\n{locator}\n{nonce}'.encode(), hashlib.sha256).hexdigest()

    async def view(self, query_id, page=None):
        result = await self.pool.view(query_id, page)
        if result['status'] != 'success':
            return FIFOExecutionResult(status='failed',tool_result=result)
        # 校验读取成功后只签发轻量引用；不把刚才渲染的正文放入工具回执或缓存。
        resource, locator, nonce = 'memory-query:'+query_id, f"page:{result['page']}", str(uuid4())
        ref = ToolPageReference(resource_id=resource,locator=locator,
            display_id=nonce+'.'+self._signature(resource,locator,nonce),media_type='text',
            description=f"历史记忆查询 {query_id}，第{result['page']}/{result['total_pages']}页，已召回{result['total']}条")
        return FIFOExecutionResult(status='succeeded', content=FoldableToolContent(pages=[ref]),
            tool_result={k:v for k,v in result.items() if k!='content'})

    async def resolve_page(self, reference):
        try:
            nonce, signature = reference.display_id.split('.',1)
            if not reference.resource_id.startswith('memory-query:') or reference.media_type!='text' or not hmac.compare_digest(
                    signature,self._signature(reference.resource_id,reference.locator,nonce)):
                raise ValueError('invalid reference')
            prefix, number = reference.locator.split(':',1)
            if prefix!='page':raise ValueError('invalid page')
            number=int(number)
        except (ValueError,AttributeError) as exc:
            raise ValueError('历史记忆页面引用无效，请重新调用查看工具。') from exc
        result = await self.pool.view(reference.resource_id.removeprefix('memory-query:'),number,advance=False)
        # 页面在展示轮内可能被驱逐，返回真实不可用提示，不伪造历史，也不阻断主循环。
        return [{'type':'text','text':result['content'] if result['status']=='success' else result['message']}]


def build_memory_view_registration(viewer):
    async def execute(operation, args):
        return await viewer.view(**args.model_dump())
    return RegisteredTool('view_memory_query',ViewMemoryQueryArguments.__doc__.replace('每页3条', f'每页{viewer.pool.page_size}条'),ViewMemoryQueryArguments,
                          execute,return_types=('foldable',))
