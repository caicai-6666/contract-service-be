"""合同类型枚举来自启动期definition；精确筛选已入库的类别code。"""
from typing import Literal
from pydantic import Field, create_model
from ..schema import RetrievalModel
from app.agent.contract_communication.agent_core.tool.registry import RegisteredTool
from .filter_execution import build_filter_handler


def compile_category_arguments(category_catalog):
    definitions = tuple(entry.definition for entry in category_catalog.categories)
    if not definitions:
        raise ValueError('合同类别目录不能为空')
    codes = tuple(definition.code for definition in definitions)
    return create_model('SearchContractsByCategoryArguments', __base__=RetrievalModel,
        category=(Literal[codes], Field(description='需要筛选的一个标准合同类型代码，使用定义中的code，不填写中文名称或自造类型。允许类型：'
            + '；'.join(f'{d.code}（{d.name}）：{d.meaning}' for d in definitions))),
        result_id=(str | None, Field(default=None, min_length=1, description='可选的当前子智能体内部候选结果集完整ID。省略或null使用初始范围；传入后仅筛选其中合同并保留排名。失效报错，不回退全库。')))


def build_category_query(arguments, scope):
    filters = [{'nested': {'path':'classification.categories', 'score_mode':'none',
        'query': {'term': {'classification.categories.code':arguments.category}}}}]
    if scope is not None:
        filters.append({'ids': {'values':list(scope)}})
    return {'bool': {'filter':filters}}


def build_contract_category_search_registration(*, results, client, index_name, metadata_store,
                                                 settings, authorize, category_catalog):
    model = compile_category_arguments(category_catalog)
    handler = build_filter_handler(results=results, client=client, index_name=index_name,
        metadata_store=metadata_store, authorize=authorize, build_query=build_category_query,
        success_message='已按入库合同类型筛选，保留历史分数和顺序；类别命中不代表具体条款已核实。')
    return RegisteredTool('search_contracts_by_category',
        '当你需要限定合同属于某种已定义类型时使用。选择一个类型，按入库类别精确筛选，不按合同名称猜测类型。'
        '同一合同可有多个类别，包含所选类型即可命中。可用result_id继续筛选已有候选；成功仅返回结果集ID和数量，零结果无ID。'
        '需要类型A或B时分别从同一初始范围查询，再调用union_contract_results；需要同时属于A和B时引用前次结果继续筛选。'
        '本工具不产生相关性分数，已有排名保持不变。', model, handler)
