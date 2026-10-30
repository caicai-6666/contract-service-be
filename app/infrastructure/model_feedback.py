"""安全的校验反馈传输与Schema机械格式化；不维护业务分支文案。"""
import json
from pydantic import ValidationError


class ModelFeedbackError(ValueError):
    """仅由校验分支构造的公开原因；禁止用原始异常或用户输入填充。"""
    def __init__(self, reason: str, action: str, state: str = '本次操作未执行，状态未改变。'):
        self.reason, self.action, self.state = reason, action, state
        super().__init__(self.public_text)

    @property
    def public_text(self):
        return f'原因：{self.reason}\n修正动作：{self.action}\n执行状态：{self.state}'


def schema_validation_feedback(error: ValidationError, model) -> str:
    """只使用Schema字段/约束和错误类型，不复制input、msg或任意ctx内容。"""
    root = model.model_json_schema()
    def expand(schema):
        if '$ref' in schema:
            node = root
            for key in schema['$ref'][2:].split('/'):
                node = node[key.replace('~1', '/').replace('~0', '~')]
            return expand(node)
        return schema
    def locate(loc):
        nodes, names = [root], []
        for part in loc:
            branches = []
            for node in nodes:
                node = expand(node)
                branches.extend(expand(b) for b in node.get('anyOf', node.get('oneOf', [node])))
            declared = [b['properties'][part] for b in branches if isinstance(part, str) and part in b.get('properties', {})]
            if declared:
                names.append(part); nodes = declared
            elif isinstance(part, int) and any('items' in b for b in branches):
                names.append('[]'); nodes = [b['items'] for b in branches if 'items' in b]
            elif any(isinstance(b.get('additionalProperties'), dict) for b in branches):
                names.append('[条目]'); nodes = [b['additionalProperties'] for b in branches if isinstance(b.get('additionalProperties'), dict)]
            else:
                # union分支标签和未知属性都不能把模型提供的名称反射进反馈。
                break
        return '.'.join(names) or '整体参数', [expand(n) for n in nodes]
    labels = {'missing':'缺少必填字段', 'extra_forbidden':'包含未声明字段',
        'string_type':'需要字符串', 'int_type':'需要严格整数', 'int_parsing':'需要整数', 'float_type':'需要数值', 'finite_number':'需要有限数值',
        'bool_type':'需要严格布尔值', 'list_type':'需要数组', 'dict_type':'需要对象',
        'literal_error':'不在允许的固定取值中', 'enum':'不在允许的枚举中',
        'string_too_short':'字符串过短', 'string_too_long':'字符串过长', 'string_pattern_mismatch':'字符串格式不匹配',
        'greater_than':'必须大于下界', 'greater_than_equal':'必须不小于下界',
        'less_than':'必须小于上界', 'less_than_equal':'必须不大于上界', 'multiple_of':'必须满足步长约束', 'too_short':'条目数量过少', 'too_long':'条目数量过多'}
    lines = []
    for issue in error.errors(include_input=False, include_url=False)[:5]:
        location, nodes = locate(issue['loc'])
        if issue['type'] == 'extra_forbidden':
            lines.append(f'{location}：包含未声明字段；删除Schema之外的属性。')
            continue
        trusted = issue.get('ctx', {}).get('error')
        if isinstance(trusted, ModelFeedbackError):
            lines.append(f'{location}：{trusted.public_text}')
            continue
        constraints = []
        for node in nodes:
            constraints.extend(f'{key}={json.dumps(node[key],ensure_ascii=False)}' for key in
                ('type','enum','const','minimum','maximum','exclusiveMinimum','exclusiveMaximum','minLength','maxLength','minItems','maxItems','multipleOf','pattern','format') if key in node)
        text = '；'.join(dict.fromkeys(constraints))
        # 业务校验不把自定义异常str传出，提供已发布字段说明作为安全修正依据。
        if issue['type'] == 'value_error':
            text = '；'.join(n['description'][:500] for n in nodes if n.get('description')) or text
        lines.append(f'{location}：{labels.get(issue["type"], "不符合字段约束")}；按字段定义修正' + (f'（{text}）' if text else '。'))
    return '\n'.join(lines)
