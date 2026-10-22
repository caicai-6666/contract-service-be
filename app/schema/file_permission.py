"""中间件文件等级调整在本平台的持久化处理结果。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FileLevelChangeHistory(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    history_id: int
    message_id: str
    target_platform_code: str
    document_id: str
    ingestion_id: str | None
    previous_level: int | None = Field(strict=True)
    new_level: int = Field(strict=True)
    reviewer: str
    note: str
    status: Literal['applied', 'skipped_missing']
    created_at: datetime
    processed_at: datetime
    acknowledged_at: datetime | None
    last_delivery_offset: int
