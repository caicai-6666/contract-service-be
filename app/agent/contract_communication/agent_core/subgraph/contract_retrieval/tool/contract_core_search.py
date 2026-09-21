"""从权威目录编译筛选 Schema 与 ES 查询，不硬编码具体业务字段。"""
from datetime import date
import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, create_model, model_validator
from app.agent.contract_communication.agent_core.tool.registry import RegisteredTool
from .filter_execution import build_filter_handler



class QueryModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)

    @model_validator(mode='after')
    def require_condition(self):
        # 所有层级都拒绝空条件；不能因遗漏或全null意外查询全库。false和0是有效值。
        if not any(value is not None for name, value in self.__dict__.items() if name != 'result_id'):
            raise ValueError('至少填写一个有效筛选条件，不能只传result_id、空对象或全null')
        return self


def _date(value):
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('日期必须为 YYYY-MM-DD')
    date.fromisoformat(value)
    return value


def compile_core_arguments(field_catalog):
    """按目录生成可选字段，每种属性仅生成一个比较对象，无条件表达式树。"""
    fields = {}
    labels = {'eq':'等于', 'gt':'大于', 'gte':'大于等于', 'lt':'小于', 'lte':'小于等于',
              'match':'分词后全部词项匹配，不保证原文精确相等'}
    for definition in field_catalog.core.definitions:
        if definition.code == 'result_id':
            raise ValueError('Core代码result_id与工具范围参数冲突')
        properties = {}
        for prop in definition.properties:
            rules = prop.constraints
            info = f'{prop.name}：{prop.meaning.strip()}。转换规则：{prop.extraction_rule}'
            if rules.enum:
                value_type = Literal[tuple(item.value for item in rules.enum)]
                info += '；允许值：' + '、'.join(f'{item.value}（{item.label}）' for item in rules.enum)
            elif prop.index_format == 'strict_date':
                value_type = Annotated[str, Field(strict=True, pattern=r'^\d{4}-\d{2}-\d{2}$'), AfterValidator(_date)]
            elif prop.type in ('number', 'integer'):
                value_type = Annotated[int if prop.type == 'integer' else float,
                    Field(strict=True, ge=rules.minimum, le=rules.maximum,
                          multiple_of=rules.multiple_of, allow_inf_nan=False)]
            elif prop.type == 'boolean':
                value_type = Annotated[bool, Field(strict=True)]
            else:
                value_type = Annotated[str, Field(strict=True, min_length=1)]
            operations = ['match'] if prop.tokenize else ['eq']
            if prop.type in ('integer', 'number') or prop.index_format == 'strict_date':
                operations += ['gt', 'gte', 'lt', 'lte']
            comparison = create_model(f'Core_{definition.code}_{prop.code}', __base__=QueryModel,
                **{op:(value_type | None, Field(default=None, description=labels[op]+'的目标值；省略或null不参与筛选。')) for op in operations})
            properties[prop.code] = (comparison | None, Field(default=None, description=info))
        if definition.cardinality == 'single' and len(properties) == 1:
            # 与ES压平规则一致，模型不用为单值字段再填写一层属性名。
            field_type = comparison
        else:
            field_type = create_model(f'CoreObject_{definition.code}', __base__=QueryModel, **properties)
            if definition.cardinality == 'multiple':
                field_type = Annotated[list[field_type], Field(min_length=1, max_length=30)]
        description = f'{definition.name}：{definition.meaning.strip()}。仅填写需要筛选的条件，全部同时满足。'
        if definition.cardinality == 'single' and len(properties) == 1:
            description += info
        elif definition.cardinality == 'multiple':
            description += '列表每项的属性必须命中同一个对象；每项都需存在匹配对象，不要求各项对象互不相同。'
        fields[definition.code] = (field_type | None, Field(default=None, description=description))
    fields['result_id'] = (str | None, Field(default=None, min_length=1,
        description='当前子智能体查询返回的完整候选结果集ID。省略或null使用初始范围；引用后继续筛选相当于AND，失效报错，不扩大范围。'))
    return create_model('SearchContractsByCoreArguments', __base__=QueryModel, **fields)


def build_core_query(arguments, field_catalog, scope):
    filters = []
    for definition in field_catalog.core.definitions:
        value = getattr(arguments, definition.code)
        if value is None:
            continue
        path = 'core.' + definition.code
        scalar = definition.cardinality == 'single' and len(definition.properties) == 1
        objects = value if definition.cardinality == 'multiple' else [value]
        for obj in objects:
            properties = {path:obj} if scalar else {
                path+'.'+prop.code:getattr(obj, prop.code) for prop in definition.properties}
            clauses = []
            for field, comparison in properties.items():
                if comparison is None:
                    continue
                for op, target in comparison.model_dump(exclude_none=True).items():
                    if op == 'match':
                        clause = {'match': {field: {'query': target, 'operator': 'and'}}}
                    elif op == 'eq':
                        clause = {'term': {field: target}}
                    else:
                        clause = {'range': {field: {op: target}}}
                    clauses.append(clause)
            query = {'bool': {'filter': clauses}}
            if definition.cardinality == 'multiple':
                query = {'nested': {'path': path, 'query': query, 'score_mode': 'none'}}
            filters.append(query)
    if scope is not None:
        filters.append({'ids': {'values': list(scope)}})
    return {'bool': {'filter': filters}}


def build_contract_core_search_registration(*, results, client, index_name, metadata_store,
                                             settings, authorize, field_catalog):
    model = compile_core_arguments(field_catalog)

    search = build_filter_handler(results=results, client=client, index_name=index_name,
        metadata_store=metadata_store, authorize=authorize,
        build_query=lambda arguments, scope: build_core_query(arguments, field_catalog, scope),
        success_message='已按入库 Core 筛选，保留历史分数和顺序；文本条件按分词匹配，不保证原文精确相等。')

    return RegisteredTool('search_contracts_by_core',
        '当你需要按已知金额、日期、主体、标的等Core条件筛选合同时使用。参数由当前目录动态生成。'
        '只选填需要筛选的Core字段，未填写或null不参与；所有条件按AND组合。数值和日期用eq/gt/gte/lt/lte，文本用match。多项字段中每个对象内条件匹配同一对象。引用上次result_id继续筛选可叠加AND；多次独立查询不会自动合并为OR结果。'
        '文本仅支持分词匹配，非原文精确相等；不要将筛选值补造成合同事实。成功仅返回候选ID与数量，零结果无ID。'
        '此工具只筛选，不引入相关性排名；有历史排名时保留其分数和顺序。', model, search)
