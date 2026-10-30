"""首次打开网页：HTTP、离线正文提取与基于关注重点的模型精炼。"""
import asyncio
from contextlib import AsyncExitStack
from copy import deepcopy
from datetime import datetime, timezone
import logging
import httpx
import lxml.html
import trafilatura

logger = logging.getLogger(__name__)


def _error(stage, code, message):
    return {'error_code': code, 'failed_stage': stage, 'hint': message}

def _no_content(message):
    # 正常无正文不是调用错误，主助手仍可换来源或说明资料缺口。
    return {'has_content': False, 'refined_content': '', 'selected_images': (),
            'hint': message, 'error_code': None, 'failed_stage': None}


from app.core.config import get_settings
from app.infrastructure.mllm import MLLMClient
from pydantic import ValidationError
from app.infrastructure.model_feedback import ModelFeedbackError, schema_validation_feedback
from app.infrastructure.model_json import load_model_json, validate_model_payload
from .schema import WebPageRequest, WebPageResult, WebPageRefinement
from .prompt import PROMPT_VERSION, build_refinement_messages
from .state import WebPageState
from .image import extract_candidates, download_images



async def fetch_html(state: WebPageState, *, timeout_seconds=30, max_bytes=3 * 1024 * 1024,
                     max_redirects=5, transport=None) -> dict:
    """流式读取解压后的 HTML；超时覆盖重定向和整个读取过程，不发布半截正文。"""
    request = WebPageRequest.model_validate(state['request'])
    metadata = {'request': request}
    try:
        async with asyncio.timeout(timeout_seconds):
            # 每次独立客户端，不携带登录凭据，也不跨网页保存 Cookie。
            async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True,
                                         max_redirects=max_redirects, transport=transport, trust_env=False) as client:
                async with client.stream('GET', str(request.url), headers={
                    'Accept': 'text/html,application/xhtml+xml',
                }) as response:
                    metadata.update(final_url=str(response.url), fetched_at=datetime.now(timezone.utc).isoformat())
                    response.raise_for_status()
                    media = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
                    if media and media not in ('text/html', 'application/xhtml+xml'):
                        return {**metadata, **_no_content(
                                '链接未返回 HTML 网页，请选择网页正文链接。')}
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            return {**metadata, **_error('fetch_html', 'response_too_large',
                                    '网页内容超过读取上限，请选择更精简的页面。')}
                        chunks.append(chunk)
                    html = b''.join(chunks)
                    if not html.strip():
                        return {**metadata, **_no_content('网页返回空内容，暂时无法读取正文，请选择其他来源。')}
                    return {**metadata, 'html': html, 'error_code': None, 'failed_stage': None, 'hint': None}
    except (TimeoutError, httpx.TimeoutException):
        return {**metadata, **_error('fetch_html', 'http_timeout', '网页读取超时，请稍后重试或选择其他来源。')}
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403, 404, 410, 451):
            message = ('该网页需要登录、验证或访问授权，暂时无法读取正文，请选择其他公开来源。'
                if exc.response.status_code in (401, 403, 451) else
                '该网页已不存在或链接已失效，暂时无法读取正文，请选择其他来源。')
            return {**metadata, **_no_content(message)}
        return {**metadata, **_error('fetch_html', 'http_status_error',
                f'网页服务器返回 HTTP {exc.response.status_code}，未取得正文，请选择其他来源。')}
    except httpx.TooManyRedirects:
        return {**metadata, **_error('fetch_html', 'too_many_redirects', '网页重定向次数过多，无法读取正文。')}
    except httpx.RequestError:
        logger.warning('网页 HTTP 读取失败')
        return {**metadata, **_error('fetch_html', 'http_request_failed', '网页网络请求失败，请稍后重试或选择其他来源。')}


def _extract(html, url, max_images):
    # 保留原始字节，让解析器依据 HTML 编码声明识别中文，避免固定 UTF-8 误解码。
    text = trafilatura.extract(html, url=url, output_format='markdown', include_links=True,
                               include_tables=True, include_comments=False)
    root = lxml.html.fromstring(html)
    for element in root.xpath('//script|//style|//noscript'):
        element.drop_tree()
    visible = ' '.join(root.text_content().split())
    # 只保留页面真实出现的限制附近原文；登录入口本身不作为熔断依据。
    clues = []
    for marker in ('登录', '登陆', '验证码', '访问受限', '权限', '订阅', 'sign in', 'log in', 'subscribe', 'captcha'):
        index = visible.lower().find(marker)
        if index >= 0:
            fragment = visible[max(0, index - 60):index + 160]
            if fragment not in clues:
                clues.append(fragment)
    return (text or '').strip(), '\n'.join(clues), extract_candidates(root, url or '', max_images)


async def extract_text(state: WebPageState, *, max_images=6) -> dict:
    """离线提取正文及访问限制线索；不将非空文本误判为有效业务内容。"""
    if not state.get('html'):
        return _no_content('页面没有可提取的正文，请选择其他来源。')
    try:
        text, access_hint, candidates = await asyncio.to_thread(_extract, state['html'], state.get('final_url'), max_images)
    except Exception:
        logger.error('网页正文提取失败')
        return _error('extract_text', 'extraction_failed', '网页正文解析失败，请选择其他来源。')
    if (not text or not any(char.isalnum() for char in text)) and not candidates:
        return {**_no_content('页面没有可提取的正文，可能需要登录或动态加载，请选择其他来源。'),
                'access_hint': access_hint}
    return {'extracted_text': text, 'access_hint': access_hint, 'image_candidates': candidates,
            'error_code': None, 'failed_stage': None, 'hint': None}


async def load_images(state: WebPageState, *, transport=None, max_bytes=4*1024*1024, resolver=None) -> dict:
    images, hint = await download_images(state.get('image_candidates', ()), transport=transport,
        max_bytes=max_bytes, resolver=resolver)
    return {'images': images, 'image_hint': hint}


async def refine_content(state: WebPageState, *, settings=None, client=None, audit=None, max_attempts=3, max_images=3) -> dict:
    """普通生成后本地验证；纠错只追加最小反馈，原始响应保留在宿主私有审计。"""
    request = WebPageRequest.model_validate(state['request'])
    text = state.get('extracted_text', '')
    images = state.get('images', ())
    if not text.strip() and not images:
        return _no_content('没有可供精炼的网页正文。')
    settings = settings or get_settings()
    records = audit if audit is not None else []
    pending_feedback = []
    if state.get('access_hint'):
        text += '\n\n页面可见访问提示：\n' + state['access_hint']
    if state.get('image_hint'):
        text += '\n\n图片读取限制：\n' + state['image_hint']
    messages = build_refinement_messages(text=text, focus=request.focus, images=images, max_images=max_images)
    try:
        async with AsyncExitStack() as stack:
            model = client if client is not None else await stack.enter_async_context(MLLMClient(settings.mllm))
            for attempt in range(1, max_attempts + 1):
                response = await model.create_chat_completion(
                    messages=deepcopy(messages),
                    max_completion_tokens=settings.mllm.generation.max_completion_tokens,
                    temperature=settings.mllm.generation.temperature,
                    enable_thinking=settings.mllm.generation.enable_thinking)
                for previous in pending_feedback:
                    previous['feedback_delivered']=True
                pending_feedback.clear()
                event = {'stage': 'refine_content', 'prompt_version': PROMPT_VERSION, 'attempt': attempt,
                         'response': response.raw_response, 'accepted': False}
                records.append(event)
                try:
                    if response.finish_reason != 'stop' or response.has_tool_calls or response.refusal:
                        raise ModelFeedbackError('网页精炼响应未完整结束、含工具或拒答标记。','本节点只接受完整JSON正文，不调用工具。')
                    output = validate_model_payload(WebPageRefinement, load_model_json(response.content))
                    selected = output.selected_images
                    # 严格检查真实看过的图片编号，不把失败下载或图注当作图像证据。
                    if (len(selected) != len(set(selected)) or len(selected) > max_images
                            or not set(selected) <= {image.number for image in images}
                            or (output.type == 'no_content' and selected)
                            or (images and 'selected_images' not in output.model_fields_set)):
                        raise ModelFeedbackError('selected_images包含未提供、重复、超量编号，或no_content携带图片。',f'只选本次实际提供的整数编号，最多{max_images}张；no_content使用空列表。')
                except (ValueError, TypeError) as exc:
                    # 不把失败的正文、推断或完整响应反灌到下一轮；私有审计完整保留。
                    event['validation_error'] = str(exc)
                    reason = (schema_validation_feedback(exc, WebPageRefinement) if isinstance(exc,ValidationError)
                        else exc.public_text if isinstance(exc,ModelFeedbackError) else '精炼正文不是完整有效的JSON对象。')
                    feedback = {'role':'user','content':reason+' 本次精炼候选未接受；按字段定义修正完整JSON。'}
                    event.update(feedback=feedback, feedback_delivered=False)
                    pending_feedback.append(event)
                    messages.append(feedback)
                    continue
                del messages[2:]
                event.update(accepted=True, message_count_after_cleanup=len(messages),
                             validated_output=output.model_dump())
                if output.type == 'no_content':
                    return _no_content(output.result)
                return {'has_content': True, 'refined_content': output.result,
                        'selected_images': tuple(image for number in selected for image in images if image.number == number),
                        'error_code': None, 'failed_stage': None, 'hint': None}
    except asyncio.CancelledError:
        records.append({'stage': 'refine_content', 'status': 'cancelled'})
        raise
    except Exception as exc:
        records.append({'stage': 'refine_content', 'error_type': type(exc).__name__})
        logger.error('网页内容精炼请求失败：error_type=%s', type(exc).__name__)
        return _error('refine_content', 'refinement_request_failed', '网页内容精炼服务暂时不可用，请稍后重试。')
    return _error('refine_content', 'invalid_model_output', '网页内容精炼未能生成有效结果，请重试；不能据此判断页面没有有效信息。')


def route_after_stage(state: WebPageState) -> str:
    """执行故障或明确无正文均提前收束，跳过余下提取步骤。"""
    return 'finalize' if state.get('error_code') or state.get('has_content') is False else 'continue'


async def finalize(state: WebPageState) -> dict:
    """区分有效正文、正常无正文与执行故障，不暴露处理半成品。"""
    request = WebPageRequest.model_validate(state['request'])
    code, stage, hint = state.get('error_code'), state.get('failed_stage'), state.get('hint')
    content = (state.get('refined_content') or '').strip()
    if not code and state.get('has_content') is False:
        return {'result': WebPageResult(status='no_content', url=str(request.url),
            final_url=state.get('final_url'), fetched_at=state.get('fetched_at'),
            hint=hint or '本次未取得可用正文，请选择其他来源或说明资料限制。')}
    if not code and (state.get('has_content') is not True or not content):
        code, stage, hint = 'invalid_processing_result', 'finalize', '网页处理结果不完整，请重试，不能据此判断没有相关信息。'
    return {'result': WebPageResult(
        status='error' if code else 'success', url=str(request.url),
        final_url=state.get('final_url'), fetched_at=state.get('fetched_at'),
        content=None if code else content, images=() if code else state.get('selected_images', ()),
        hint=hint if code else (state.get('image_hint') or None), error_code=code, failed_stage=stage)}
