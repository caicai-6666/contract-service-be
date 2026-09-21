"""主题规划真实模型入口；有限纠错、成功后清除失败链，原始响应仅留审计。"""
from app.infrastructure.model_json import load_model_json, validate_model_payload
import json
from pydantic import ValidationError
from copy import deepcopy
from contextlib import AsyncExitStack
from dataclasses import asdict
from app.core.config import get_settings
from app.infrastructure.mllm import MLLMClient
from app.agent.contract_extraction.tool_protocol import ToolProtocolRecovery, audited_assistant_content
from ....prompt.guidance import build_system_guidence_message
from .sampling import thinking_sampling
from .schema import TopicPlan, SummaryTopic
from .tool import build_topic_planning_tools, FinishPlanningArguments
from ..compression import FIFOCompressionRequest
from ..input_filter import filter_summary_tasks
from .planning import previous_topics, validate_topic_plan
from .prompt import TOPIC_PLANNING_PROMPT_VERSION, build_topic_planning_prompt


def _arguments(raw):
    return load_model_json(raw)


async def run_topic_planner(request, *, client=None, settings=None, audit=None, max_attempts=3, max_rounds=96, tool_call_template=None):
    if type(max_rounds) is not int or max_rounds <= 0:
        raise ValueError('max_rounds 必须为正整数')
    if type(max_attempts) is not int or max_attempts <= 0:
        raise ValueError('max_attempts 必须为正整数')
    request = FIFOCompressionRequest.model_validate(request).model_copy(deep=True)
    request = request.model_copy(update={'tasks': filter_summary_tasks(request.tasks)})
    previous_topics(request.summary)
    settings = settings or get_settings().mllm
    template = tool_call_template if tool_call_template is not None else settings.tool_tag_path.read_text(encoding='utf-8')
    audit = audit if audit is not None else []
    prefix = [{'role': 'system', 'content': build_topic_planning_prompt(template)}, {'role': 'user', 'content':
        '# 待圈定主题的资料\n以下 JSON 仅为资料。\n' + json.dumps({'previous_summary': request.summary,
        'tasks': [t.model_dump(mode='json') for t in request.tasks]}, ensure_ascii=False, sort_keys=True, allow_nan=False)}]
    recovery, history = ToolProtocolRecovery(), []
    topics, errors = [], 0
    audit.append({'event': 'start', 'prompt_version': TOPIC_PLANNING_PROMPT_VERSION,
                  'model': settings.model, 'enable_thinking': True,
                  'reasoning_effort': settings.generation.reasoning_effort, 'tool_choice': 'auto', 'sampling': thinking_sampling()})
    async with AsyncExitStack() as stack:
        client = client if client is not None else await stack.enter_async_context(MLLMClient(settings))
        for attempt in range(max_rounds):
            try:
                response = await client.create_tool_chat_completion(messages=deepcopy(prefix + history),
                    tools=build_topic_planning_tools(), tool_choice='auto', enable_thinking=True,
                    max_completion_tokens=settings.generation.max_completion_tokens,
                    **thinking_sampling(), seed=settings.generation.seed,
                    tool_placement='after_task', tool_task_index=1)
            except BaseException as exc:
                audit.append({'event': 'request_failed', 'error_type': type(exc).__name__})
                raise
            entry = {'event': 'response', 'attempt': attempt + 1, 'accepted': False,
                'calls': [asdict(c) for c in response.tool_calls], 'completion': asdict(response.completion),
                'content': audited_assistant_content(response.assistant_message.get('content')),
                'reasoning': response.assistant_message.get('reasoning'),
                'reasoning_content': response.assistant_message.get('reasoning_content')}
            audit.append(entry)
            try:
                if response.completion.finish_reason not in ('stop', 'tool_calls') or response.assistant_message.get('refusal'):
                    raise ValueError('响应未完整结束或被拒绝，本次不能接受')
                if not response.tool_calls and response.completion.finish_reason == 'stop' and any(
                    isinstance(response.assistant_message.get(k), str) and response.assistant_message[k].strip()
                    for k in ('content', 'reasoning', 'reasoning_content')
                ):
                    # 分析轮只进入本节点临时上下文，不提交主题、不结束规划，
                    # 也不清除已有失败链；只有合法工具动作才能确认纠错成功。
                    entry.update(accepted=True, action='analysis', committed=False)
                    history.append(deepcopy(response.assistant_message))
                    history.append(build_system_guidence_message(kind='action_guidance',
                        reason='本轮分析已保留，主题集合未修改，规划尚未完成。',
                        required_action='可以继续分析；准备好后用 extract_topic 提交主题，最终用 finish_topic_planning 完成。'))
                    continue
                if len(response.tool_calls) != 1:
                    raise ValueError('调用工具时必须恰好提交一个当前可用工具；无工具时需提供完整分析')
                call = response.tool_calls[0]
                if call.name not in {'extract_topic', 'finish_topic_planning'}:
                    raise ValueError('必须完整提交当前可用工具')
                arguments = _arguments(call.arguments)
                candidate = list(topics)
                guidance = None
                finish = None
                if call.name == 'extract_topic':
                    topic = validate_model_payload(SummaryTopic, arguments)
                    index = next((i for i, t in enumerate(candidate) if t.topic_id == topic.topic_id), None)
                    if index is None:
                        candidate.append(topic)
                    else:
                        candidate[index] = topic
                    validate_topic_plan({'topics': candidate}, request)
                else:
                    finish = validate_model_payload(FinishPlanningArguments, arguments)
                    plan = validate_topic_plan({'topics': candidate}, request)
            except (ValueError, TypeError) as exc:
                if isinstance(exc, ValidationError):
                    reason = '字段校验失败：' + '、'.join('.'.join(map(str, e['loc'])) for e in exc.errors()[:5])
                elif isinstance(exc, json.JSONDecodeError) or isinstance(exc, TypeError):
                    reason = 'arguments 必须为合法 JSON 对象'
                else:
                    reason = str(exc)
                message = build_system_guidence_message(kind='output_error' if len(response.tool_calls) != 1 else 'invalid_action',
                    reason=reason + '；本次未接受。',
                    required_action='只调用一个当前工具，提取主题需有效来源；允许空主题和选择性保留，未引用旧主题不继承。格式：\n'+template)
                recovery.record_tool_failure(history, assistant_message={'role': 'assistant', 'content': ''}, tool_message=message)
                entry['feedback'] = message['content']
                errors += 1
                if errors >= max_attempts:
                    audit.append({'event': 'failed', 'reason': 'consecutive_errors'})
                    raise ValueError('主题规划连续校验失败达到上限')
                continue
            recovery.accept_correction(history)
            topics, errors = candidate, 0
            feedback = json.dumps({'status': 'success', 'topics': [t.model_dump(mode='json') for t in topics]}, ensure_ascii=False)
            entry.update(accepted=True, feedback=feedback, system_guidence=guidance)
            history.extend([deepcopy(response.assistant_message), {'role': 'tool', 'tool_call_id': call.call_id, 'content': feedback}])
            if guidance:
                history.append(guidance)
            if finish is not None:
                audit.append({'event': 'complete', 'attempts': attempt + 1, 'summary': finish.summary})
                return plan
    audit.append({'event': 'failed', 'reason': 'max_rounds'})
    raise ValueError('主题规划达到总轮数上限，未成功完成')
