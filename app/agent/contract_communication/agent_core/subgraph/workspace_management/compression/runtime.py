"""压缩子 Agent 的有界工具循环；会话轨迹、候选和私有审计相互隔离。"""
from app.core.tool_tag import resolve_mllm_tool_tag, tool_tag_section
from app.infrastructure.model_json import load_model_json, validate_model_payload
from contextlib import AsyncExitStack
from copy import deepcopy
from dataclasses import asdict
from inspect import isawaitable
import json
from time import perf_counter

from pydantic import ValidationError
from app.core.config import get_settings
from app.infrastructure.mllm import MLLMClient
from app.schema.communication_workspace import WorkspaceSnapshot, WorkspacePayload
from app.agent.contract_extraction.tool_protocol import ToolProtocolRecovery, audited_assistant_content
from app.agent.contract_communication.agent_core.prompt.guidance import build_system_guidence_message
from app.agent.contract_communication.agent_core.tool.workspace import prepare_workspace_edit
from ..token_count import count_workspace_tokens, render_workspace, WORKSPACE_RENDER_VERSION
from .prompt import WORKSPACE_COMPRESSION_PROMPT_VERSION, build_workspace_compression_prompt, build_workspace_compression_task_prompt
from app.infrastructure.model_feedback import ModelFeedbackError, schema_validation_feedback
from .tool import FinishCompressionArguments, build_compression_tools


class CompressionLoopError(ModelFeedbackError):
    """压缩未完成，禁止发布循环中的候选副本。"""


def _parse_arguments(raw, model):
    return validate_model_payload(model, load_model_json(raw))


async def run_compression_tool_loop(request, *, client=None, settings=None, counter=None,
                                    audit=None, max_rounds=32, max_consecutive_errors=3,
                                    tool_call_template=None):
    """完成工具验收通过后返回候选，否则抛错；外部注入的客户端由调用方关闭。

    audit 为调用方持有的私有追加列表，不放入消息或工作区。每个 invocation
    创建独立轨迹和纠错边界；原始参数只留私有审计。
    """
    from .agent import validate_compression_candidate
    for value in (max_rounds, max_consecutive_errors):
        if type(value) is not int or value <= 0:
            raise ValueError('压缩轮数和连续错误上限必须是正整数')
    settings = settings or get_settings().mllm
    audit = audit if audit is not None else []
    template = resolve_mllm_tool_tag(settings, tool_call_template)
    tools = build_compression_tools()
    prefix = [{'role': 'system', 'content': build_workspace_compression_prompt(tool_call_template=template)},
              {'role': 'user', 'content': build_workspace_compression_task_prompt(request)}]
    history = []
    recovery = ToolProtocolRecovery(tool_call_template=template)
    consecutive_errors = 0
    workspace = request.workspace.model_copy(deep=True)

    async def count(value):
        result = counter(value.model_copy(deep=True)) if counter else count_workspace_tokens(value, settings=settings)
        result = await result if isawaitable(result) else result
        if type(result) is not int or result < 0:
            raise CompressionLoopError('工作区计数不可用。','停止整理并等待计数服务恢复，不通过猜测容量继续修改。')
        return result

    def capacity_feedback(current):
        usage = f'当前使用量：{current}/{request.token_budget} token（{current / request.token_budget:.1%}）。'
        # 提示与实际finish校验同用target_tokens，不从独立文案模块复制业务阈值。
        if current > request.target_tokens:
            return usage + f'尚未达到压缩目标，请整理至最多{request.target_tokens} token（严格低于70%），暂不能完成。'
        return usage + '容量已经安全，核对事实、来源与保护项后优先调用finish_compression；不为更低使用率过度整理。'

    pending_feedback = []
    tokens = await count(workspace)
    audit.append({'event': 'start', 'prompt_version': WORKSPACE_COMPRESSION_PROMPT_VERSION,
                  'render_version': WORKSPACE_RENDER_VERSION, 'model': settings.model,
                  'tokens': tokens, 'target_tokens': request.target_tokens,
                  'enable_thinking': True, 'reasoning_effort': settings.generation.reasoning_effort})
    async with AsyncExitStack() as stack:
        client = client if client is not None else await stack.enter_async_context(MLLMClient(settings))
        for round_number in range(1, max_rounds + 1):
            # 末尾只放一份最新工作区。纠错修改 history，不会移动固定工具锚点。
            messages = deepcopy(prefix + history + [{'role': 'user', 'content':
                '# 最新工作区\n\n' + capacity_feedback(tokens) + '\n\n' + render_workspace(workspace)}])
            started = perf_counter()
            try:
                response = await client.create_tool_chat_completion(
                    messages=messages, tools=deepcopy(tools), tool_choice='auto',
                    max_completion_tokens=settings.generation.max_completion_tokens,
                    temperature=settings.generation.temperature, top_p=settings.generation.top_p,
                    top_k=settings.generation.top_k, presence_penalty=settings.generation.presence_penalty,
                    repetition_penalty=settings.generation.repetition_penalty, seed=settings.generation.seed,
                    enable_thinking=True, tool_placement='after_task', tool_task_index=1)
            except BaseException as exc:
                audit.append({'event': 'request_failed', 'round': round_number, 'error_type': type(exc).__name__})
                raise
            for pending in pending_feedback:
                pending['feedback_delivered'] = True
            pending_feedback.clear()
            entry = {'event': 'tool_round', 'round': round_number, 'elapsed_ms': (perf_counter()-started)*1000,
                     'completion': asdict(response.completion), 'calls': [asdict(call) for call in response.tool_calls],
                     'assistant_content': audited_assistant_content(response.assistant_message.get('content')),
                     'accepted': False}
            audit.append(entry)
            feedback = None
            if len(response.tool_calls) != 1:
                # 不回显普通文本或伪调用；共享恢复器负责标记连续失败范围。
                recovery.record_protocol_failure(history, assistant_message={'content': ''},
                    tool_call_count=len(response.tool_calls), result_label='工作区压缩操作')
                history[-1] = build_system_guidence_message(kind='output_error',
                    reason=f'本次响应解析到 {len(response.tool_calls)} 个工具调用，必须恰好一个；本次未执行操作。',
                    required_action='只通过原生tool_calls提交一个当前工具。' + tool_tag_section(template))
                feedback = history[-1]['content']
            else:
                call = response.tool_calls[0]
                try:
                    if (response.completion.finish_reason not in ('stop', 'tool_calls')
                            or response.assistant_message.get('refusal')):
                        raise ModelFeedbackError('响应未完整结束或包含拒答标记。','提交完整原生工具调用，截断参数不能执行。')
                    if call.name == 'finish_compression':
                        completed = _parse_arguments(call.arguments, FinishCompressionArguments)
                        if tokens > request.target_tokens:
                            raise ModelFeedbackError(capacity_feedback(tokens),'先精简未受保护的冗余内容，达到目标后再调用finish_compression。')
                        candidate = validate_compression_candidate(request, workspace)
                        next_tokens = tokens
                        success_feedback = f'压缩完成。当前使用量：{tokens}/{request.token_budget} token（{tokens / request.token_budget:.1%}）。'
                    else:
                        edit = prepare_workspace_edit(WorkspaceSnapshot(payload=workspace, revision=0, updated_at=0), call.name, call.arguments)
                        candidate = validate_compression_candidate(request, edit.payload)
                        # 压缩不能假装完成业务探索，也不能使用原操作预留的键。
                        if call.name == 'workspace_add' and edit.removed_path:
                            raise ModelFeedbackError('压缩不能执行探索收束。','保持规划/结果归属，不把剩余方向转入已探索区。')
                        if candidate == workspace:
                            raise ModelFeedbackError('操作未改变工作区，无法推进压缩。','选择实际冗余内容精简，不重复提交相同值。')
                        next_tokens = await count(candidate)
                        success_feedback = '工作区修改成功。' + capacity_feedback(next_tokens)
                except (ValueError, TypeError, KeyError) as exc:
                    if isinstance(exc, ModelFeedbackError):
                        feedback = exc.public_text
                    elif isinstance(exc, ValidationError):
                        feedback = schema_validation_feedback(exc, FinishCompressionArguments if call.name=='finish_compression' else WorkspacePayload)
                    else:
                        feedback = 'arguments或目标路径无效：使用有效JSON对象和最新工作区内允许的路径。'
                    message = build_system_guidence_message(kind='invalid_action', reason=feedback+' 压缩候选未提交。',
                        required_action='按具体原因修正；本次失败修改未生效，不重复同一非法操作。')
                    recovery.record_tool_failure(history, assistant_message={'role': 'assistant', 'content': ''}, tool_message=message)
                except BaseException as exc:
                    entry['error_type'] = type(exc).__name__
                    raise
                else:
                    recovery.accept_correction(history)
                    workspace, tokens = candidate, next_tokens
                    consecutive_errors = 0
                    entry.update(accepted=True, feedback=success_feedback, tokens=tokens)
                    accepted_message = deepcopy(response.assistant_message)
                    accepted_message['content'] = None
                    history.extend([accepted_message, {'role': 'tool', 'tool_call_id': call.call_id, 'content': success_feedback}])
                    if call.name == 'finish_compression':
                        audit.append({'event': 'complete', 'tokens': tokens, 'rounds': round_number, 'summary': completed.summary})
                        return workspace
                    continue
            entry.update(feedback=feedback, feedback_delivered=False)
            pending_feedback.append(entry)
            consecutive_errors += 1
            if consecutive_errors >= max_consecutive_errors:
                audit.append({'event': 'failed', 'reason': 'consecutive_errors'})
                raise CompressionLoopError('自动压缩连续错误达到上限。最后反馈未发送给子模型。'+feedback, '依据最后安全诊断调整原操作或保留必要信息后重试；不要声称压缩已完成。')
    audit.append({'event': 'failed', 'reason': 'max_rounds'})
    raise CompressionLoopError('自动压缩达到最大调用轮数，尚未验收完成。','调整精简方案后重试，原工作区未修改。')
