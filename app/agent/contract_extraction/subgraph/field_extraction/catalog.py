"""Core 与 Attribute 字段定义的启动期全量加载器。"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Literal

import yaml
from pydantic import ValidationError

from app.agent.contract_extraction.subgraph.field_extraction.definition import (
    FieldDefinition,
    FieldDefinitionCatalog,
    FieldDefinitionCollection,
)

FieldDefinitionKind = Literal["core", "attribute"]


class FieldDefinitionCatalogError(ValueError):
    """字段目录无法形成可信内存快照。"""


def _fingerprint(root: Path, paths: tuple[Path, ...]) -> str:
    """把相对路径和原始字节共同纳入确定性内容指纹。"""
    digest = sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _load_definition(path: Path) -> FieldDefinition:
    """读取一个 YAML，并把解析或 Schema 错误定位到具体文件。"""
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise FieldDefinitionCatalogError(
            f"无法读取字段定义 {path}：{exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise FieldDefinitionCatalogError(f"字段定义 {path} 的顶层必须是对象")
    try:
        return FieldDefinition.model_validate(payload)
    except ValidationError as exc:
        raise FieldDefinitionCatalogError(
            f"字段定义 {path} 不符合 Schema：{exc}"
        ) from exc


def _load_collection(
    root: Path,
    *,
    kind: FieldDefinitionKind,
) -> tuple[FieldDefinitionCollection, tuple[Path, ...]]:
    """按文件名稳定加载一个职责目录，Attribute 允许为空。"""
    directory = root / kind
    if not directory.is_dir():
        raise FieldDefinitionCatalogError(f"字段定义目录不存在：{directory}")
    entries = sorted(directory.iterdir(), key=lambda path: path.name)
    invalid = [
        entry.name
        for entry in entries
        if not entry.is_file()
        or (entry.suffix != ".yaml" and entry.name != ".gitkeep")
    ]
    if invalid:
        raise FieldDefinitionCatalogError(
            f"字段定义目录 {directory} 包含非 YAML 条目：{invalid}"
        )
    paths = tuple(entry for entry in entries if entry.suffix == ".yaml")
    if kind == "core" and not paths:
        raise FieldDefinitionCatalogError("Core 字段定义目录不能为空")

    definitions = tuple(_load_definition(path) for path in paths)
    names = [definition.name for definition in definitions]
    if len(names) != len(set(names)):
        raise FieldDefinitionCatalogError(f"{kind} 字段定义名称不能重复")
    return (
        FieldDefinitionCollection(
            kind=kind,
            definitions=definitions,
            content_sha256=_fingerprint(root, paths),
        ),
        paths,
    )


def load_field_definition_catalog(root: Path) -> FieldDefinitionCatalog:
    """全量加载 Core 与 Attribute；任一错误都会阻止应用启动。"""
    resolved_root = root.resolve()
    if not resolved_root.is_dir():
        raise FieldDefinitionCatalogError(
            f"字段定义根目录不存在或不是目录：{resolved_root}"
        )
    actual_entries = {entry.name for entry in resolved_root.iterdir()}
    expected_entries = {"core", "attribute"}
    if actual_entries != expected_entries:
        missing = sorted(expected_entries - actual_entries)
        unexpected = sorted(actual_entries - expected_entries)
        problems: list[str] = []
        if missing:
            problems.append(f"缺少 {missing}")
        if unexpected:
            problems.append(f"存在未定义条目 {unexpected}")
        raise FieldDefinitionCatalogError(
            f"字段定义根目录布局错误：{'；'.join(problems)}"
        )

    core, core_paths = _load_collection(resolved_root, kind="core")
    attribute, attribute_paths = _load_collection(
        resolved_root,
        kind="attribute",
    )
    core_names = {definition.name for definition in core.definitions}
    duplicate_names = sorted(
        core_names.intersection(
            definition.name for definition in attribute.definitions
        )
    )
    if duplicate_names:
        raise FieldDefinitionCatalogError(
            f"Core 与 Attribute 字段定义名称重复：{duplicate_names}"
        )

    return FieldDefinitionCatalog(
        root=resolved_root,
        core=core,
        attribute=attribute,
        content_sha256=_fingerprint(
            resolved_root,
            core_paths + attribute_paths,
        ),
    )


__all__ = [
    "FieldDefinitionCatalogError",
    "load_field_definition_catalog",
]
