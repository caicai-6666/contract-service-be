"""单节点多轮检索Agent：设置条件→执行；成功即返回，失败轨迹只用于连续纠错。"""
from contextlib import AsyncExitStack
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json

from app.core.config import get_settings
from app.core.tool_tag import get_mllm_tool_tag
from app.infrastructure.mllm import MLLMClient
from app.agent.contract_extraction.tool_protocol import ToolProtocolRecovery
from ...prompt.guidance import build_system_guidence_message
from .schema import MemoryRetrievalRequest, MemoryRetrievalResult
from .session import MemoryRetrievalSession
from .tool import build_memory_retrieval_tools, execute_memory_retrieval_tool
from .prompt import build_memory_retrieval_prompt, MEMORY_RETRIEVAL_PROMPT_VERSION


def failure(code, message):
    return MemoryRetrievalResult(status='error',error_code=code,error=message)


async def run_memory_retrieval(request, *, result_pool, client=None, settings=None, encoder=None,
                               audit=None, max_rounds=16, max_errors=3, tool_template=None):
    if type(max_rounds) is not int or max_rounds<1 or type(max_errors) is not int or max_errors<1:
        raise ValueError('轮数与连续错误上限必须为正整数')
    request = MemoryRetrievalRequest.model_validate(request)
    if request.conversation_id != result_pool.conversation_id:
        return failure('invalid_scope','查询会话与结果池不一致。')
    settings = settings or get_settings().mllm
    template = tool_template if tool_template is not None else get_mllm_tool_tag()
    session = MemoryRetrievalSession(request,database=result_pool.database,encoder=encoder,result_pool=result_pool)
    stamp = datetime.fromtimestamp(request.reference_time/1000,timezone(timedelta(hours=8))).isoformat()
    # 任务变量不混入稳定系统前缀；JSON字符串编码防止正文伪造字段边界。
    prefix = [{'role':'system','content':build_memory_retrieval_prompt(template)},
              {'role':'user','content':'# 本次记忆检索\n参考时间（北京时间）：'+stamp+
               '\n检索需求：'+json.dumps(request.query,ensure_ascii=False)+
               '\n初始查询条件：'+json.dumps(session.conditions.model_dump(),ensure_ascii=False)}]
    audit = audit if audit is not None else []
    audit.append({'event':'start','prompt_version':MEMORY_RETRIEVAL_PROMPT_VERSION,'model':settings.model})
    history, recovery, errors = [], ToolProtocolRecovery(), 0
    generation=settings.generation
    async with AsyncExitStack() as stack:
        model=client if client is not None else await stack.enter_async_context(MLLMClient(settings))
        for round_number in range(1,max_rounds+1):
            try:
                response=await model.create_tool_chat_completion(messages=deepcopy(prefix+history),
                    tools=build_memory_retrieval_tools(),tool_choice='auto',enable_thinking=True,
                    max_completion_tokens=generation.max_completion_tokens,temperature=generation.temperature,
                    top_p=generation.top_p,top_k=generation.top_k,presence_penalty=generation.presence_penalty,
                    repetition_penalty=generation.repetition_penalty,seed=generation.seed,
                    tool_placement='after_task',tool_task_index=1)
            except Exception as exc:
                audit.append({'event':'generation_failed','error_type':type(exc).__name__})
                return failure('model_unavailable','记忆检索模型暂时无法完成请求，请稍后重试。')
            event={'round':round_number,'accepted':False,'assistant':deepcopy(response.assistant_message),
                   'calls':[asdict(call) for call in response.tool_calls],'completion':asdict(response.completion)}
            audit.append(event)
            valid = response.completion.finish_reason in ('stop','tool_calls') and not response.assistant_message.get('refusal')
            if not valid or len(response.tool_calls)!=1:
                feedback={'status':'error','message':'响应未完整结束或未提供恰好一个合法工具调用。'}
            else:
                call=response.tool_calls[0]
                feedback=await execute_memory_retrieval_tool(session,call.name,call.arguments)
            event['feedback']=deepcopy(feedback)
            if feedback['status']=='error':
                if feedback.get('error_code') in {'query_execution_failed','query_busy'}:
                    return failure(feedback['error_code'],feedback.get('error') or feedback.get('message'))
                errors+=1
                guidance=build_system_guidence_message(kind='invalid_action',reason=json.dumps(feedback,ensure_ascii=False),
                    required_action='依据字段说明修正；本轮只调用一个实际提供的工具。调用格式：\n'+template)
                recovery.record_tool_failure(history,assistant_message={'role':'assistant','content':''},tool_message=guidance)
                if errors>=max_errors:
                    return failure('correction_limit','记忆检索连续调用错误达到上限，本次查询未完成。')
                continue
            recovery.accept_correction(history)
            errors=0;event['accepted']=True
            if call.name=='execute_query':
                audit.append({'event':'complete','query_id':session.result.query_id})
                return session.result
            # 不保留成功调用的长思考；当前条件以程序回执为准，私有审计保留原响应。
            history.extend([{'role':'assistant','content':None,'tool_calls':[{
                'id':call.call_id,'type':'function','function':{'name':call.name,'arguments':call.arguments}}]},
                {'role':'tool','tool_call_id':call.call_id,'content':json.dumps(feedback,ensure_ascii=False)}])
    return failure('round_limit','记忆检索达到调用轮数上限，尚未执行成功。')
