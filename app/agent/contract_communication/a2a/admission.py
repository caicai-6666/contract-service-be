"""首次请求的单动作受理判断；不经过用户业务门禁，不执行合同查询。"""
from copy import deepcopy
from functools import partial
from app.infrastructure.vllm_tokenizer import count_chat_tokens
from dataclasses import asdict
import json
from pydantic import Field, ValidationError
from app.infrastructure.model_feedback import ModelFeedbackError, schema_validation_feedback
from app.schema.communication_workspace import WorkspaceObject, WorkspaceText
from app.core.tool_tag import resolve_mllm_tool_tag
from app.agent.contract_extraction.tool_protocol import ToolProtocolRecovery
from ..agent_core.prompt import build_system_guidence_message, build_agent_core_interaction_prompt
from ..agent_core.tool.registry import RegisteredTool, ToolRegistry
from ..agent_core.subgraph.fifo_management.schema import FIFOExecutionResult
from .prompt.role import A2A_ROLE

ADMISSION_PROMPT_VERSION = 'a2a-admission-v1'


class AcceptTaskArguments(WorkspaceObject):
    accepted: bool = Field(strict=True, description='是否受理当前请求；属于本平台合同业务且可协作时为true，明显不属于服务范围时为false。不以材料暂缺替代业务范围判断。')
    message: WorkspaceText = Field(max_length=10000, description='向调用方简要说明能协助的事项或不受理原因；可说明下一轮需提供的资料，不执行任务、不声称已查询或核对合同，不包含私有思考。')


async def decide_admission(content, *, client, settings, audit, tool_call_template=None, max_attempts=3, chat_counter=None):
    settings = settings.for_a2a_admission()
    template = resolve_mllm_tool_tag(settings, tool_call_template)
    counter = chat_counter or partial(count_chat_tokens, settings=settings, chat_template_kwargs={
        'enable_thinking':settings.generation.enable_thinking, 'preserve_thinking':True,
        'tool_placement':'before_task', 'tool_task_index':1})
    async def unused(*args): return FIFOExecutionResult(status='succeeded', tool_result={})
    registry = ToolRegistry([RegisteredTool('accept_task', '判断是否受理本次合同协作请求，并立即提交简要反馈。', AcceptTaskArguments, unused)])
    messages = [{'role':'system','content': A2A_ROLE + '\n\n' + build_agent_core_interaction_prompt(tool_call_template=template)},
        {'role':'user','content':content}, build_system_guidence_message(kind='action_guidance',
            reason='这是本次协作的首次请求，请先判断是否属于你能协助的合同业务。',
            required_action='现在仅调用accept_task提交受理判断与简要说明。不要开始查询或处理；确认受理后，等待下一条明确请求继续。材料暂缺但属于合同业务时，可受理并说明需要补充什么。例如核对合同付款条款可以受理并请求合同线索；明显与合同无关的任务应不受理并说明服务范围。')]
    temporary = []
    pending_feedback = []
    recovery = ToolProtocolRecovery(tool_call_template=template)
    for attempt in range(1, max_attempts+1):
        tokens = await counter([*messages,*temporary], registry.definitions())
        if tokens + settings.generation.max_completion_tokens > settings.context_window_tokens:
            raise ValueError('首次判断超过上下文容量')
        response = await client.create_tool_chat_completion(messages=deepcopy([*messages,*temporary]),
            tools=registry.definitions(), tool_choice={'type':'function','function':{'name':'accept_task'}},
            max_completion_tokens=settings.generation.max_completion_tokens, temperature=settings.generation.temperature,
            top_p=settings.generation.top_p, top_k=settings.generation.top_k,
            presence_penalty=settings.generation.presence_penalty,repetition_penalty=settings.generation.repetition_penalty,
            seed=settings.generation.seed, enable_thinking=settings.generation.enable_thinking,
            preserve_thinking=True, tool_placement='before_task', tool_task_index=1)
        for pending in pending_feedback:
            pending['feedback_delivered'] = True
        pending_feedback.clear()
        event={'stage':'admission','attempt':attempt,'prompt_version':ADMISSION_PROMPT_VERSION,'reasoning_effort':settings.generation.reasoning_effort,
            'response':deepcopy(response.assistant_message),'completion':asdict(response.completion),'accepted':False}
        audit.append(event)
        try:
            if len(response.tool_calls)!=1:
                raise ModelFeedbackError(f'本轮解析到{len(response.tool_calls)}个原生调用，无法提交认领判断。', '只调用一次accept_task提交accepted和message。')
            if response.tool_calls[0].name!='accept_task':
                raise ModelFeedbackError('认领阶段调用了未提供的工具。','仅使用accept_task，后续业务处理等待单聊请求。')
            if response.completion.finish_reason not in ('stop','tool_calls') or response.assistant_message.get('refusal'):
                raise ModelFeedbackError('认领响应未完整结束或包含拒答标记。','重新完整提交accept_task；不受理时accepted=false并简述原因。')
            call=response.tool_calls[0]
            from app.infrastructure.model_json import load_model_json, validate_model_payload
            value=validate_model_payload(AcceptTaskArguments,load_model_json(call.arguments))
            assistant={'role':'assistant','content':None,'tool_calls':[{'id':call.call_id,'type':'function',
                'function':{'name':call.name,'arguments':call.arguments}}]}
            if settings.generation.enable_thinking:
                assistant.update({k:v for k,v in response.assistant_message.items() if k in ('reasoning','reasoning_content') and isinstance(v,str) and v.strip()})
            from ..agent_core.native_messages import validate_native_messages
            exchanges=validate_native_messages([assistant,{'role':'tool','tool_call_id':call.call_id,
                'content':json.dumps({'status':'success','accepted':value.accepted},ensure_ascii=False)}])
        except (ValueError,TypeError) as exc:
            if isinstance(exc, ValidationError):
                reason = schema_validation_feedback(exc, AcceptTaskArguments)
            elif isinstance(exc, ModelFeedbackError):
                reason = exc.public_text
            else:
                reason = 'arguments必须是完整JSON对象，禁止重复键和非有限数值。'
            guidance = build_system_guidence_message(kind='output_error', reason=reason+' 本次认领结果未提交。',
                required_action='只调用一次accept_task，accepted使用严格布尔值，message使用1至10000字符说明。')
            temporary.append(guidance)
            event.update(feedback=guidance, feedback_delivered=False)
            pending_feedback.append(event)
            continue
        temporary.clear()
        recovery.accept_correction(temporary)
        event['accepted']=True
        return value, exchanges
    raise ValueError('受理判断未能生成有效结果')
