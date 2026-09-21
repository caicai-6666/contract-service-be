"""任务三入口检索存储契约；不生成文本、不调用模型。"""
import math
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, model_validator

RETRIEVAL_DIMENSIONS = 4096
Vector = tuple[Annotated[float, Field(strict=True)], ...]


class TaskRetrievalRecord(BaseModel):
    """一份任务的三组文本与向量；原任务及消息顺序由record_id回连。"""
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)

    record_id: str = Field(min_length=1, description='conversation_records中终态任务的record_id，不是turn_id。')
    user_input_text: str | None = Field(default=None, description='格式化用户问题及文件名、展示名称、文件摘要；不含file_id和page_count元数据。')
    user_input_embedding: Vector | None = Field(default=None, description='用户文字及每份文件格式化内容分别编码、归一化、等权平均后再次归一化的4096维融合向量。')
    intermediate_output_text: str | None = Field(default=None, description='按原顺序组织的已完成公开中途输出正文，不含内部思考或工具结果。')
    intermediate_output_embedding: Vector | None = Field(default=None, description='各条中途输出分别归一化、等权平均后再次归一化的4096维融合向量。')
    final_output_text: str | None = Field(default=None, description='实际完成的最终答复原文；没有最终输出时为空，不生成替代结论。')
    final_output_embedding: Vector | None = Field(default=None, description='最终输出文本的4096维L2归一化向量。')

    @model_validator(mode='after')
    def validate_pairs(self):
        for prefix in ('user_input','intermediate_output','final_output'):
            text=getattr(self,prefix+'_text'); vector=getattr(self,prefix+'_embedding')
            if (text is None) != (vector is None):
                raise ValueError('每个入口的文本与向量必须同时存在或同时为空')
            if text is not None and not text.strip():
                raise ValueError('检索正文不能是空字符串或空白')
            if vector is not None and (len(vector)!=RETRIEVAL_DIMENSIONS or
                    not all(math.isfinite(v) for v in vector) or
                    not math.isclose(math.hypot(*vector),1.0,rel_tol=1e-6,abs_tol=1e-6)):
                raise ValueError('检索向量必须为4096维有限值且L2归一化')
        return self
