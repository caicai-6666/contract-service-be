"""共用值约束；调用方负责基本类型校验，再按业务路径包装错误。"""
from decimal import Decimal
from .definition import FieldPropertyDefinition


def constraint_violation(definition: FieldPropertyDefinition, value) -> str | None:
    rules = definition.constraints
    if rules.enum is not None and value not in {item.value for item in rules.enum}:
        options = '、'.join(f'{item.value}（{item.label}）' for item in rules.enum)
        return f'仅允许 {options}；收到 {value!r}'
    if rules.minimum is not None and value < rules.minimum:
        return f'数值必须大于等于 {rules.minimum:g}；收到 {value!r}'
    if rules.maximum is not None and value > rules.maximum:
        return f'数值必须小于等于 {rules.maximum:g}；收到 {value!r}'
    if rules.multiple_of is not None and Decimal(str(value)) % Decimal(str(rules.multiple_of)) != 0:
        return f'数值必须为 {rules.multiple_of:g} 的整数倍；收到 {value!r}，不得四舍五入凑值'
    return None
