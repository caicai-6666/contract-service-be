"""合同业务只读定义的 HTTP 响应契约。"""

from pydantic import BaseModel, ConfigDict, Field, RootModel

from app.agent.contract_extraction.subgraph.field_extraction.definition import (
    FieldCardinality,
    FieldDefinitionCatalog,
    FieldValueType,
)


class ContractSchemaModel(BaseModel):
    """合同定义接口共用的严格不可变模型。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CorePropertyDefinitionResponse(ContractSchemaModel):
    """前端构造一个 Core 属性输入控件所需的定义。"""

    code: str = Field(description="属性在 Core 对象中的稳定英文键。")
    name: str = Field(description="属性供审核人员阅读的中文名称。")
    type: FieldValueType = Field(description="属性允许提交的 JSON 基本类型。")
    required: bool = Field(description="该属性在非空 Core 对象中是否必填。")


class CoreFieldDefinitionResponse(ContractSchemaModel):
    """前端构造一个 Core 字段审核区域所需的定义。"""

    code: str = Field(description="字段在 Core 中的稳定英文键。")
    name: str = Field(description="字段供审核人员阅读的中文名称。")
    cardinality: FieldCardinality = Field(
        description="single 表示单项，multiple 表示可增删的多项列表。"
    )
    properties: tuple[CorePropertyDefinitionResponse, ...] = Field(
        min_length=1,
        description="字段每一项需要填写的扁平属性定义。",
    )


class CoreDefinitionCatalogResponse(
    RootModel[tuple[CoreFieldDefinitionResponse, ...]]
):
    """按启动期目录顺序返回的 Core 表单定义列表。"""


def project_core_definition_catalog(
    catalog: FieldDefinitionCatalog,
) -> CoreDefinitionCatalogResponse:
    """从完整业务定义中筛出前端生成审核表单所需的信息。"""
    return CoreDefinitionCatalogResponse(
        root=tuple(
            CoreFieldDefinitionResponse(
                code=definition.code,
                name=definition.name,
                cardinality=definition.cardinality,
                properties=tuple(
                    CorePropertyDefinitionResponse(
                        code=property_definition.code,
                        name=property_definition.name,
                        type=property_definition.type,
                        required=property_definition.required,
                    )
                    for property_definition in definition.properties
                ),
            )
            for definition in catalog.core.definitions
        )
    )


__all__ = [
    "CoreDefinitionCatalogResponse",
    "CoreFieldDefinitionResponse",
    "CorePropertyDefinitionResponse",
    "project_core_definition_catalog",
]
