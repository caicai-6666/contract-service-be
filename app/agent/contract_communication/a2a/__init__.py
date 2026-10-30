"""以任务为单位的跨平台交互准备入口，不注册 HTTP/SSE 路由。"""
from .factory import run_a2a_task
from .model import A2ATaskRequest, A2ATaskResult
from .task import InMemoryA2ATask

__all__ = ['run_a2a_task', 'A2ATaskRequest', 'A2ATaskResult', 'InMemoryA2ATask']
