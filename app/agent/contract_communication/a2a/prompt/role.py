"""跨平台任务处理角色；工程版本不注入模型。"""
from ...agent_core.prompt.role import build_agent_core_role_prompt

A2A_ROLE_VERSION = 'a2a-role-v1'
A2A_ROLE = build_agent_core_role_prompt(
    '你是现象合同智能管理平台的合同业务助手，接收调用方交付的合同业务任务，围绕合同及相关材料获取信息、理解约定、分析问题并交付处理结果。')
