"""可选开发链路：仅在任务局部捕获，不进入模型记忆或生产 SSE。"""
from contextvars import ContextVar
from datetime import datetime, timezone
from pydantic import BaseModel

current_trace = ContextVar('communication_development_trace', default=None)


def safe_value(value):
    # 图片只保留占位，避免把多页 Base64 复制到调试页面。
    if isinstance(value, BaseModel):
        return safe_value(value.model_dump(mode='json'))
    if isinstance(value, dict):
        return {str(k): '[图像内容省略]' if k in {'image_url', 'image', 'b64_json'} else safe_value(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_value(v) for v in value]
    if isinstance(value, str):
        return '[内嵌媒体省略]' if value.startswith('data:') else value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return '[非文本对象省略]'


def record(kind, title, **values):
    records = current_trace.get()
    if records is None:
        return None
    entry = dict(kind=kind, title=title, recorded_at=datetime.now(timezone.utc).isoformat(),
                 **safe_value(values))
    # 限制开发观察记录；不影响实际任务或其私有审计。
    if len(records) < 2000:
        records.append(entry)
    return entry
