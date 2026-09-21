"""单主题强制 JSON 生成；身份由程序补齐，失败只留私有审计。"""
from app.infrastructure.model_json import validate_model_payload
import json
from contextlib import AsyncExitStack
from copy import deepcopy

from pydantic import ValidationError
from app.core.config import get_settings
from app.infrastructure.mllm import MLLMClient
from ....prompt.guidance import build_system_guidence_message
from ..input_filter import filter_summary_tasks
from .planning import previous_topics
from .rendering import render_topic_generation_input, INPUT_RENDER_VERSION
from .planner import _arguments
from .sampling import thinking_sampling
from .prompt import TOPIC_GENERATION_PROMPT, TOPIC_GENERATION_PROMPT_VERSION
from .schema import TopicGenerationRequest, TopicGenerationOutput, TopicSummary, topic_generation_json_schema


async def run_topic_generator(request, *, client=None, settings=None, audit=None, max_attempts=3):
    if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
        raise ValueError('单主题生成允许 1 至 3 次尝试')
    request = TopicGenerationRequest.model_validate(request).model_copy(deep=True)
    request = request.model_copy(update={'tasks': filter_summary_tasks(request.tasks)})
    ids = [task.task_id for task in request.tasks]
    if (len(ids) != len(set(ids)) or set(ids) != set(request.topic.task_ids)
            or len(request.topic.task_ids) != len(set(request.topic.task_ids))
            or len(request.topic.previous_topic_ids) != len(set(request.topic.previous_topic_ids))
            or set(previous_topics(request.previous_summary)) != set(request.topic.previous_topic_ids)):
        raise ValueError('单主题输入来源与规划不一致')
    settings = settings or get_settings().mllm
    audit = audit if audit is not None else []
    messages = [{'role': 'system', 'content': TOPIC_GENERATION_PROMPT},
                {'role': 'user', 'content': render_topic_generation_input(request)}]
    audit.append({'event': 'start', 'prompt_version': TOPIC_GENERATION_PROMPT_VERSION,
                  'topic_id': request.topic.topic_id, 'model': settings.model, 'input_render_version': INPUT_RENDER_VERSION,
                  'enable_thinking': True, 'reasoning_effort': settings.generation.reasoning_effort, 'sampling': thinking_sampling()})
    try:
        async with AsyncExitStack() as stack:
            client = client if client is not None else await stack.enter_async_context(MLLMClient(settings))
            for attempt in range(1, max_attempts + 1):
                response = await client.create_json_chat_completion(
                    messages=deepcopy(messages), json_schema=topic_generation_json_schema(),
                    schema_name='fifo_topic_generation',
                    enable_thinking=True, **thinking_sampling(),
                    max_completion_tokens=settings.generation.max_completion_tokens)
                record = {'event': 'response', 'attempt': attempt, 'accepted': False,
                          'response': deepcopy(response.raw_response)}
                audit.append(record)
                try:
                    if response.refusal or response.has_tool_calls or response.finish_reason != 'stop':
                        raise ValueError('需要正常结束的完整 JSON，不能拒答、调用工具或提交截断结果。')
                    raw = _arguments(response.content)
                    if not isinstance(raw, dict) or list(raw) != ['summary']:
                        raise ValueError('JSON 仅包含 summary，不输出 reasoning 或其他字段。')
                    output = validate_model_payload(TopicGenerationOutput, raw)
                    for field in type(output.summary).model_fields:
                        for item in getattr(output.summary, field):
                            if len(item.task_ids) != len(set(item.task_ids)) or not set(item.task_ids) <= set(ids):
                                raise ValueError('条目 task_ids 必须是不重复的本主题任务引用。')
                    summary = TopicSummary(topic_id=request.topic.topic_id, title=request.topic.title,
                                           **output.summary.model_dump())
                except (ValueError, TypeError) as exc:
                    # 不回显失败正文；保留稳定前缀，仅追加最小修复方向。
                    if isinstance(exc, ValidationError):
                        reason = '字段校验失败：' + '、'.join('.'.join(map(str, e['loc'])) for e in exc.errors()[:5])
                    elif isinstance(exc, (json.JSONDecodeError, TypeError)):
                        reason = '响应必须为完整合法的 JSON 对象。'
                    else:
                        reason = str(exc)
                    feedback = build_system_guidence_message(kind='output_error', reason=reason,
                        required_action='最终JSON只包含summary及其current_memory、open_items两个列表；不生成topic_id、title或其他字段，不调用工具。')
                    record['feedback'] = feedback
                    if attempt < max_attempts:
                        messages.append(feedback)
                    continue
                del messages[2:]
                record.update(accepted=True, message_count_after_cleanup=len(messages))
                return summary
        raise ValueError('单主题摘要连续校验失败达到上限')
    except BaseException as exc:
        audit.append({'event': 'failed', 'error_type': type(exc).__name__})
        raise
    finally:
        del messages[2:]
