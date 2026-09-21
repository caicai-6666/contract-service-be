"""合同查询子图节点：业务对齐与多轮检索；权威结果均由程序校验。"""
from contextlib import AsyncExitStack
from copy import deepcopy
from dataclasses import asdict
import json
from app.core.tool_tag import get_mllm_tool_tag
from app.infrastructure.mllm import MLLMClient
from app.infrastructure.model_json import load_model_json, validate_model_payload
from app.agent.contract_extraction.subgraph.field_extraction.tool import _validate_property_value
from app.core.contract_date import normalize_contract_date
from app.agent.contract_extraction.tool_protocol import ToolProtocolRecovery
from app.agent.contract_communication.agent_core.prompt.guidance import build_system_guidence_message
from app.agent.contract_communication.agent_core.tool.registry import ToolRegistry
from app.agent.contract_communication.agent_core.subgraph.fifo_management.schema import FIFOOperation
from .schema import ContractRetrievalRequest,ContractRetrievalResult,QueryAlignmentGeneration
from .session import CandidatePool
from app.agent.contract_extraction.subgraph.classification.catalog import load_contract_category_catalog
from .tool.contract_category_search import build_contract_category_search_registration
from .tool.union import build_union_contract_results_registration
from .tool.finish import build_finish_contract_retrieval_registration
from .prompt import (build_prompt, CONTRACT_RETRIEVAL_PROMPT_VERSION, render_business_definitions,
                     ALIGNMENT_SYSTEM_PROMPT, ALIGNMENT_PROMPT_VERSION)
from .tool.contract_name_search import build_contract_name_search_registration
from .tool.contract_summary_search import build_contract_summary_search_registration
from .tool.contract_note_search import build_contract_note_search_registration
from .tool.contract_core_search import build_contract_core_search_registration
from app.agent.contract_extraction.subgraph.field_extraction.catalog import load_field_definition_catalog
from .tool.contract_clause_search import build_contract_clause_search_registration
from .tool.contract_question_search import build_contract_question_search_registration


def apply_alignment(query, generation, field_catalog, category_catalog):
    """目录值和替换位置均由程序校验；无关原文不交给模型重新生成。"""
    if not generation.can_align:
        return None
    fields = {d.code: d for d in field_catalog.core.definitions}
    categories = {c.definition.code: c.definition for c in category_catalog.categories}
    edits = []
    for edit in generation.evidence:
        if query.count(edit.original) != 1:
            raise ValueError(f'原片段必须在查询中唯一出现：{edit.original!r}')
        start = query.index(edit.original)
        if edit.kind == 'category':
            if edit.code not in categories or edit.property_code is not None or edit.values:
                raise ValueError('类别必须来自目录，property_code 为 null，values 为空')
            if edit.code not in edit.aligned:
                raise ValueError('类别标准表述必须包含对应类别 code')
        else:
            field = fields.get(edit.code)
            prop = next((p for p in field.properties if p.code == edit.property_code), None) if field else None
            if prop is None:
                raise ValueError(f'不存在 Core 属性 {edit.code}.{edit.property_code}')
            for value in edit.values:
                _validate_property_value(prop, value)
                if prop.index_format == 'strict_date' and normalize_contract_date(value) != value:
                    raise ValueError('日期标准值必须为 YYYY-MM-DD')
                if prop.constraints.enum is not None and value not in edit.aligned:
                    raise ValueError('标准表述必须包含枚举 value，不能只写标签')
        edits.append((start, start + len(edit.original), edit.aligned))
    edits.sort()
    if any(left[1] > right[0] for left, right in zip(edits, edits[1:])):
        raise ValueError('替换片段不能重叠，请缩小为独立条件')
    aligned = query
    for start, end, replacement in reversed(edits):
        aligned = aligned[:start] + replacement + aligned[end:]
    # 同样接受后续请求的长度约束，不能把异常长的生成带入查询。
    if generation.result != aligned:
        raise ValueError("result 必须等于按 evidence 局部转换得到的完整查询；不得修改其他内容")
    return ContractRetrievalRequest(query=aligned).query


async def align_contract_query(state, *, settings, authorize, field_catalog, category_catalog, client=None, audit=None):
    request = ContractRetrievalRequest.model_validate(state['request'])
    records = audit if audit is not None else []
    messages = [{'role':'system', 'content':ALIGNMENT_SYSTEM_PROMPT},
                {'role':'user', 'content':render_business_definitions(field_catalog,category_catalog)+'\n\n# 原始查询\n'+json.dumps(request.query,ensure_ascii=False)}]
    try:
        await authorize()
        async with AsyncExitStack() as stack:
            model = client if client is not None else await stack.enter_async_context(MLLMClient(settings.mllm))
            for attempt in range(1, 4):
                await authorize()
                response = await model.create_json_chat_completion(messages=deepcopy(messages),
                    json_schema=QueryAlignmentGeneration.model_json_schema(), schema_name='contract_query_alignment',
                    max_completion_tokens=settings.mllm.generation.max_completion_tokens, enable_thinking=True)
                event = {'stage':'alignment','prompt_version':ALIGNMENT_PROMPT_VERSION,'attempt':attempt,
                         'response':response.raw_response,'accepted':False}
                records.append(event)
                try:
                    if response.refusal or response.has_tool_calls or response.finish_reason != 'stop':
                        raise ValueError('必须正常结束并返回完整 JSON，不允许工具调用或拒答标记')
                    generation = validate_model_payload(QueryAlignmentGeneration, load_model_json(response.content))
                    aligned = apply_alignment(request.query,generation,field_catalog,category_catalog)
                except (ValueError, TypeError) as exc:
                    event['feedback'] = str(exc)
                    messages.append({'role':'user','content':'对齐输出未通过校验，请修正：'+str(exc)})
                    continue
                await authorize()
                del messages[2:]
                event.update(accepted=True, message_count_after_cleanup=len(messages))
                if not generation.can_align:
                    return {'alignment_passed':False,'result':ContractRetrievalResult(status='error',
                        error=generation.result,explanation='查询条件未通过业务对齐，尚未执行检索。')}
                return {'alignment_passed':True,
                    'aligned_request':request.model_copy(update={'query':aligned}),
                    'alignment':generation}
    except Exception as exc:
        records.append({'stage':'alignment','error_type':type(exc).__name__})
    return {'alignment_passed':False,'result':ContractRetrievalResult(status='error',
        error='查询条件对齐未能完成，请稍后重试；本次尚未执行合同检索。')}



def build_query_tools(*,pool,metadata_store,settings,authorize,es_client,field_catalog=None,category_catalog=None):
    options=dict(results=pool,metadata_store=metadata_store,settings=settings,authorize=authorize)
    tools=[build_contract_name_search_registration(**options),build_contract_summary_search_registration(**options),build_contract_note_search_registration(**options)]
    if es_client is not None:
        field_catalog = field_catalog or load_field_definition_catalog(settings.field_definition_path)
        category_catalog = category_catalog or load_contract_category_catalog(settings.contract_category_definition_path)
        tools.append(build_contract_category_search_registration(**options,client=es_client,index_name=settings.elasticsearch_index_name,category_catalog=category_catalog))
        tools.append(build_contract_core_search_registration(**options,client=es_client,index_name=settings.elasticsearch_index_name,field_catalog=field_catalog))
        tools.append(build_contract_clause_search_registration(**options,client=es_client,index_name=settings.elasticsearch_index_name))
        tools.append(build_contract_question_search_registration(**options,client=es_client,index_name=settings.elasticsearch_index_name))
    tools.append(build_union_contract_results_registration(pool=pool,authorize=authorize))
    return tools


async def run_contract_retrieval(request,*,metadata_store,settings,authorize,es_client=None,client=None,audit=None,tool_template=None,field_catalog=None,category_catalog=None):
    request=ContractRetrievalRequest.model_validate(request)
    # Core目录只用于编译工具参数，不再重复渲染到查询system中。
    field_catalog = field_catalog or load_field_definition_catalog(settings.field_definition_path)
    pool=CandidatePool(metadata_store,initial_document_ids=request.initial_document_ids,initial_ranking=request.initial_ranking,rrf_k=settings.communication_contract_retrieval_rrf_k,history_weight=settings.communication_contract_retrieval_history_weight,capacity=settings.communication_contract_search_cache_max_queries)
    audit=audit if audit is not None else []
    final=None
    def accept_final(value):
        nonlocal final
        final=value
    tools=build_query_tools(pool=pool,metadata_store=metadata_store,settings=settings,authorize=authorize,es_client=es_client,field_catalog=field_catalog,category_catalog=category_catalog)
    tools.append(build_finish_contract_retrieval_registration(pool=pool,authorize=authorize,on_complete=accept_final))
    registry=ToolRegistry(tools);handlers=registry.handlers()
    template=tool_template if tool_template is not None else get_mllm_tool_tag()
    prefix=[{'role':'system','content':build_prompt(template)},
        {'role':'user','content':'# 查找需求\n'+json.dumps(request.query,ensure_ascii=False)+'\n初始范围：'+('全部ready合同' if request.initial_document_ids is None else f'宿主限定的{len(request.initial_document_ids)}份合同，禁止扩大')}]
    history=[];errors=0;recovery=ToolProtocolRecovery()
    generation=settings.mllm.generation
    audit.append({'event':'start','prompt_version':CONTRACT_RETRIEVAL_PROMPT_VERSION})
    try:
        await authorize()
        # 显式空范围无需调用模型，也不误降级全库。
        if request.initial_document_ids==():return ContractRetrievalResult(status='success',explanation='初始候选范围为空。')
        async with AsyncExitStack() as stack:
            model=client if client is not None else await stack.enter_async_context(MLLMClient(settings.mllm))
            for round_number in range(1,settings.communication_contract_retrieval_max_rounds+1):
                await authorize()
                response=await model.create_tool_chat_completion(messages=deepcopy(prefix+history),tools=registry.definitions(),
                    tool_choice='auto',enable_thinking=True,max_completion_tokens=generation.max_completion_tokens,
                    temperature=generation.temperature,top_p=generation.top_p,top_k=generation.top_k,
                    presence_penalty=generation.presence_penalty,repetition_penalty=generation.repetition_penalty,
                    seed=generation.seed,tool_placement='after_task',tool_task_index=1)
                event={'round':round_number,'assistant':deepcopy(response.assistant_message),'calls':[asdict(c) for c in response.tool_calls],
                    'completion':asdict(response.completion),'accepted':False}
                audit.append(event)
                call=response.tool_calls[0] if len(response.tool_calls)==1 else None
                valid=(call is not None and response.completion.finish_reason in ('stop','tool_calls')
                    and not response.assistant_message.get('refusal') and not response.assistant_message.get('content') and call.name in handlers)
                if valid:
                    await authorize()
                    result=await handlers[call.name](FIFOOperation(task_id='contract-retrieval',call_id=call.call_id,name=call.name,arguments=call.arguments))
                    feedback=result.tool_result;success=result.status=='succeeded'
                else:
                    feedback={'status':'error','message':'请只调用一个当前提供的工具，不附带普通正文；响应必须完整结束。'};success=False
                event['feedback']=deepcopy(feedback)
                if not success:
                    errors+=1
                    recovery.record_tool_failure(history,assistant_message={'role':'assistant','content':''},
                        tool_message=build_system_guidence_message(kind='invalid_action',reason=json.dumps(feedback,ensure_ascii=False),required_action='修正后只调用一个合法工具。\n'+template))
                    if errors>=3:return ContractRetrievalResult(status='error',error='连续调用错误达到上限，本次查询未完成。')
                    continue
                recovery.accept_correction(history);errors=0;event['accepted']=True
                if call.name=='finish_contract_retrieval':
                    await authorize()
                    return final
                # 内部不渲染名单，不保留推理；权威状态只来自成功执行回执。
                history.extend([{'role':'assistant','content':None,'tool_calls':[{'id':call.call_id,'type':'function','function':{'name':call.name,'arguments':call.arguments}}]},
                    {'role':'tool','tool_call_id':call.call_id,'content':json.dumps(feedback,ensure_ascii=False)}])
        return ContractRetrievalResult(status='error',error='查询达到轮数上限，尚未形成最终候选列表。')
    except Exception as exc:
        audit.append({'event':'failed','error_type':type(exc).__name__})
        return ContractRetrievalResult(status='error',error='合同候选查询失败，请稍后重试；不能据此判断没有相关合同。')
    finally:
        pool.close()


async def retrieve_contracts(state, **options):
    """查询节点接收已对齐请求，将收束结果写回子图状态。"""
    return {'result': await run_contract_retrieval(state['aligned_request'], **options)}
