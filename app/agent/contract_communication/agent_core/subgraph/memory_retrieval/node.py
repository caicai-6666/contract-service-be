"""单个多轮检索节点；异常不作为虚假的成功空结果。"""
from pydantic import ValidationError
from .agent import run_memory_retrieval, failure


async def retrieve_memory(state, *, result_pool, **options):
    try:
        result = await run_memory_retrieval(state.get('request'), result_pool=result_pool, **options)
    except ValidationError:
        result = failure('invalid_request','记忆检索请求不合法，请检查查询内容及参考时间。')
    return {'result':result}
