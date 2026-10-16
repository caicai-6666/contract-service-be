"""通行证集合的规范化；不解析或改写通行证业务值。"""
from typing import Annotated

from pydantic import BeforeValidator


def normalize_passports(value: object) -> tuple[str, ...]:
    """兼容旧标量；保留顺序、重复项和首尾空白，返回不可变集合。"""
    if isinstance(value, str):
        value = (value,) if value else ()
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError('passport 必须为字符串数组，每项为非空白字符串')
    return tuple(value)


Passports = Annotated[tuple[str, ...], BeforeValidator(normalize_passports)]
