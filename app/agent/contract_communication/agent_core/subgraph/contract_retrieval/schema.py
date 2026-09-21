"""合同候选查询公开契约，内部范围由宿主注入，不由子模型改写。"""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictStr, StrictInt, StrictFloat, StrictBool

class RetrievalModel(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True,str_strip_whitespace=True)

class ContractRankingItem(RetrievalModel):
    document_id: str = Field(pattern=r'^[0-9a-f]{64}$', description='候选合同完整 ID。')
    score: float = Field(allow_inf_nan=False, description='当前用于排序的综合分数。')
    raw_score: float | None = Field(default=None, allow_inf_nan=False, description='最近一次相关性查询的原始分数；union类型为该次并集RRF分数，不代表任一路原分；旧结果缺失时以score为准。')
    search_type: str = Field(description='最近一次相关性查询方式、并集融合 union，或首次精确过滤 exact。')
    score_kind: Literal['raw','rrf','filter'] = Field(default='raw', description='综合分数为原始分值、跨查询 RRF 或无相关性的过滤标记。')
    has_relevance: bool = Field(default=True, description='当前排序是否具有相关性证据；首次纯过滤为 false。')


class ContractRetrievalRequest(RetrievalModel):
    initial_ranking: tuple[ContractRankingItem,...] | None = Field(default=None, description='宿主传入的初始结果排名快照；不向模型公开或接受模型修改。')
    query: str=Field(min_length=1,max_length=10000,description='用户希望查找的合同条件，保留未知与不确定性。')
    initial_document_ids: tuple[str,...]|None=Field(default=None,description='宿主解析的初始合同范围；None全库，空元组为空范围，模型不得扩大。')

class ContractCandidate(ContractRankingItem):
    file_name: str=Field(description='当前数据库合同名称。')
    summary: str|None=Field(default=None,description='当前数据库摘要，未记录为空。')

class ContractRetrievalResult(RetrievalModel):
    status: Literal['success','error']
    candidates: tuple[ContractCandidate,...]=()
    explanation: str=''
    error: str|None=None


class QueryAlignmentEdit(RetrievalModel):
    original: str = Field(min_length=1, description='原查询中需要对齐的连续原文片段，必须唯一出现，不能包含无关条件。')
    kind: Literal['core', 'category'] = Field(description='对齐到 Core 属性或合同类别。')
    code: str = Field(min_length=1, description='所给目录中的 Core 或类别 code，禁止创造代码。')
    property_code: str | None = Field(description='Core 属性 code；类别对齐必须为 null。')
    values: tuple[StrictStr | StrictInt | StrictFloat | StrictBool, ...] = Field(description='Core 条件中的标准值，保持原文顺序；只提及属性无具体值时为空。类别对齐必须为空。')
    aligned: str = Field(min_length=1, description='替换原片段的标准表述，保留运算关系、否定、单位、角色绑定和不确定性；不得混入其他条件。')


class QueryAlignmentGeneration(RetrievalModel):
    evidence: tuple[QueryAlignmentEdit, ...] = Field(description='支撑对齐的原查询片段、定义引用与标准值；无需对齐时为空。只列与 Core 或类别相关的内容。')
    reasoning: str = Field(min_length=1, description='结合原查询和权威定义说明对齐依据、转换规则或无法对齐的原因。')
    can_align: StrictBool = Field(description='相关条件都能可靠对齐时为 true，否则为 false；无相关条件时原样通过。')
    result: str = Field(min_length=1, max_length=10000, description='can_align=true 时为完整对齐后查询，其他内容原样保留；false 时为具体、友好的错误提示，说明需要澄清或修正的条件。')
