"""模型结构化输出的传输兼容；不修复语法、不替代业务校验。"""
import json
from math import isfinite
from typing import Any, TypeVar

from pydantic import BaseModel

Model = TypeVar('Model', bound=BaseModel)


def load_model_json(raw: str) -> Any:
    """严格读取一层 JSON，内嵌层同样拒绝重复键和非标准数值。"""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('模型 JSON 包含重复字段')
            result[key] = value
        return result

    def invalid(value):
        raise ValueError('模型 JSON 不允许非标准数值')

    def finite_float(value):
        number = float(value)
        if not isfinite(number):
            raise ValueError('模型 JSON 数值超出有限范围')
        return number

    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid, parse_float=finite_float)


def normalize_model_json(value: Any, schema: dict, *, root: dict | None = None, depth: int = 0) -> Any:
    """仅对期望对象/数组的字段解码；字符串及无类型字段原样保留。

    union 允许字符串时优先保留文本；调用方可依据业务判别字段缩小 Schema。
    返回新容器，不改写原始响应或私有审计。最终合法性仍由原校验器负责。
    """
    if depth > 64:
        raise ValueError('模型 JSON 嵌套超过兼容转换上限')
    root = schema if root is None else root
    if '$ref' in schema:
        ref = schema['$ref']
        if not ref.startswith('#/'):
            raise ValueError('模型 JSON Schema 仅支持本地引用')
        resolved = root
        for part in ref[2:].split('/'):
            resolved = resolved[part.replace('~1', '/').replace('~0', '~')]
        return normalize_model_json(value, resolved, root=root, depth=depth + 1)
    branches = schema.get('anyOf', schema.get('oneOf'))
    if branches:
        # 展开引用以判断允许的 JSON 类型；不猜测字符串究竟是否意在提交对象。
        def kind(branch):
            if '$ref' in branch:
                target = root
                for part in branch['$ref'][2:].split('/'):
                    target = target[part.replace('~1', '/').replace('~0', '~')]
                return target.get('type')
            return branch.get('type')
        if isinstance(value, str) and any(kind(b) in (None, 'string') for b in branches):
            return value
        if isinstance(value, str) and any(kind(b) in ('object', 'array') for b in branches):
            decoded = load_model_json(value)
            return normalize_model_json(decoded, schema, root=root, depth=depth + 1)
        expected = 'object' if isinstance(value, dict) else 'array' if isinstance(value, list) else None
        candidates = [b for b in branches if expected and kind(b) == expected]
        if len(candidates) == 1:
            return normalize_model_json(value, candidates[0], root=root, depth=depth + 1)
        # 多个对象分支只在产生一致结果时采用转换，避免擅自选中错误分支。
        if candidates:
            results = [normalize_model_json(value, b, root=root, depth=depth + 1) for b in candidates]
            if all(result == results[0] for result in results):
                return results[0]
        return value
    expected = schema.get('type')
    if isinstance(value, str) and expected in ('object', 'array'):
        return normalize_model_json(load_model_json(value), schema, root=root, depth=depth + 1)
    if isinstance(value, dict) and expected == 'object':
        properties = schema.get('properties', {})
        additional = schema.get('additionalProperties', {})
        return {key: normalize_model_json(item, properties.get(key, additional) if isinstance(
            properties.get(key, additional), dict) else {}, root=root, depth=depth + 1)
            for key, item in value.items()}
    if isinstance(value, list) and expected == 'array':
        prefix = schema.get('prefixItems', [])
        return [normalize_model_json(item, prefix[index] if index < len(prefix) else schema.get('items', {}),
                    root=root, depth=depth + 1) for index, item in enumerate(value)]
    return value


def validate_model_payload(model: type[Model], payload: Any, **kwargs) -> Model:
    """用于工具参数：兼容传输后继续执行原 Pydantic Python 校验。"""
    return model.model_validate(normalize_model_json(payload, model.model_json_schema()), **kwargs)


def validate_model_json(model: type[Model], raw: str, **kwargs) -> Model:
    """用于强制 JSON 输出：保留原有 JSON 模式（如严格 tuple/date）的语义。"""
    payload = normalize_model_json(load_model_json(raw), model.model_json_schema())
    return model.model_validate_json(json.dumps(payload, ensure_ascii=False, allow_nan=False), **kwargs)
