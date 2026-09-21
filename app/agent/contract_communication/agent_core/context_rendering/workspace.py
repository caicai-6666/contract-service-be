"""主助手工作区展示：省略空区域，保留已有条目的真实键、值与引用关系。"""
import re

import yaml

from app.schema.communication_workspace import WorkspacePayload

WORKSPACE_SECTION_RENDER_VERSION = 'agent-core-workspace-render-v2'
_REGIONS = (
    ('task_constraints', '任务约束'),
    ('known_information', '已知信息'),
    ('explored_directions', '已探索方向'),
    ('remaining_directions', '剩余规划'),
)


class _WorkspaceDumper(yaml.SafeDumper):
    """仅影响此渲染器，不修改 PyYAML 全局序列化行为。"""


def _represent_text(dumper, value):
    # 多行正文采用块样式；序列化器仍负责空白、类型歧义和特殊字符转义。
    return dumper.represent_scalar('tag:yaml.org,2002:str', value,
                                   style='|' if '\n' in value else None)


_WorkspaceDumper.add_representer(str, _represent_text)


def _yaml_block(value: dict) -> str:
    text = yaml.dump(value, Dumper=_WorkspaceDumper, allow_unicode=True,
                     sort_keys=False, default_flow_style=False, width=100)
    # 围栏长度超过资料中的连续反引号，避免正文伪造区域边界。
    fence = '`' * max(3, 1 + max((len(run) for run in re.findall(r'`+', text)), default=0))
    return f'{fence}yaml\n{text}{fence}'


def render_workspace_section(workspace: WorkspacePayload | dict) -> str:
    """返回工作区展示；省略空区域，全部为空时返回简短提示，非法结构显式拒绝。

    只接受四区域 payload，不猜测快照、旧版迁移、容量或存储状态。
    快照调用方应传入 snapshot.payload；不会修改输入或重新编号条目。
    """
    if isinstance(workspace, WorkspacePayload):
        workspace = workspace.model_dump(mode='json')
    payload = WorkspacePayload.model_validate(workspace).model_dump(mode='json')
    # 只过滤区域与任务约束的空值，不能递归删掉条目中的 source=null 或空引用列表。
    constraints = payload['task_constraints']
    payload['task_constraints'] = {
        key: value for key, value in constraints.items()
        if (key == 'task' and value is not None) or (key == 'supplements' and value)
    }
    sections = [f'## {title}\n\n路径：/{key}\n\n{_yaml_block(payload[key])}'
                for key, title in _REGIONS if payload[key]]
    if not sections:
        return '当前工作区为空。'
    boundary = '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━'
    return (f'{boundary}\n当前工作区\n{boundary}\n\n'
            + '\n\n────────────────────────────────────────\n\n'.join(sections)
            + '\n\n━━━━━━━━━━━━ 当前工作区结束 ━━━━━━━━━━━━')
