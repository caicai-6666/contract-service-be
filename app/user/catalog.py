"""审核用户 YAML 文件的启动期加载器。"""

from pathlib import Path

import yaml
from pydantic import ValidationError

from app.user.model import ReviewerUserCatalog, ReviewerUserFile


class ReviewerUserCatalogError(ValueError):
    """审核用户文件无法形成可信内存快照。"""


def load_reviewer_user_catalog(path: Path) -> ReviewerUserCatalog:
    """加载并严格校验审核用户文件，任一错误都会阻止应用启动。"""
    resolved_path = path.resolve()
    if not resolved_path.is_file():
        raise ReviewerUserCatalogError(
            f"审核用户文件不存在或不是文件：{resolved_path}"
        )
    if resolved_path.suffix != ".yaml":
        raise ReviewerUserCatalogError(
            f"审核用户文件必须使用 .yaml 扩展名：{resolved_path}"
        )

    try:
        raw_content = resolved_path.read_bytes()
        payload = yaml.safe_load(raw_content.decode("utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ReviewerUserCatalogError(
            f"无法读取审核用户文件 {resolved_path}：{exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ReviewerUserCatalogError(
            f"审核用户文件 {resolved_path} 的顶层必须是对象"
        )

    try:
        definition = ReviewerUserFile.model_validate(payload)
        return ReviewerUserCatalog.from_file(
            resolved_path,
            definition,
            raw_content,
        )
    except ValidationError as exc:
        raise ReviewerUserCatalogError(
            f"审核用户文件 {resolved_path} 不符合 Schema：{exc}"
        ) from exc


__all__ = [
    "ReviewerUserCatalogError",
    "load_reviewer_user_catalog",
]
