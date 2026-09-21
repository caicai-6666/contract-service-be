"""组装侧的临时页面展示：前置提醒由程序添加，工具只提供页面引用。"""
from collections.abc import Mapping
from copy import deepcopy
import json

from ..prompt.guidance import build_system_guidence_message
from ..subgraph.fifo_management.schema import FIFOExecutionResult

PAGE_DISPLAY_RENDER_VERSION = 'agent-core-page-display-v2'


def render_tool_page_messages(
    result: FIFOExecutionResult, *, visible_pages: Mapping[str, list[dict]], remaining_rounds: int = 5,
) -> list[dict]:
    """将本次真正展示的页面组装为独立提示和资料消息，不修改工具回执。

    visible_pages 由展示窗口提供，键为 display_id，值为资源解析后
    的标准 text/image_url 内容块；缺席项表示本次不展示，不由此函数推断其生命周期。
    本函数不加载资源、不消耗展示机会，也不把页面加入持久化轨迹。
    """
    if type(remaining_rounds) is not int or remaining_rounds < 1:
        raise ValueError('剩余展示轮数必须为正整数')
    result = FIFOExecutionResult.model_validate(result)
    if result.content.type == 'ordinary':
        if visible_pages:
            raise ValueError('普通结果不能注入页面内容')
        return []
    references = {page.display_id: page for page in result.content.pages}
    if not set(visible_pages) <= set(references):
        raise ValueError('展示内容必须来自本次工具返回的页面引用')
    blocks = []
    for ref in result.content.pages:
        if ref.display_id not in visible_pages:
            continue
        values = visible_pages[ref.display_id]
        if not isinstance(values, list) or not values:
            raise ValueError('待展示页面必须具有实际内容')
        expected = 'image_url' if ref.media_type == 'image' else 'text'
        for block in values:
            if not isinstance(block, dict) or block.get('type') != expected:
                raise ValueError('页面内容块与声明的展示形式不一致')
            if expected == 'text':
                if set(block) != {'type', 'text'} or not isinstance(block['text'], str) or not block['text'].strip():
                    raise ValueError('文本页面必须为非空 text 内容块')
            else:
                image = block.get('image_url')
                if (set(block) != {'type', 'image_url'} or not isinstance(image, dict)
                        or not isinstance(image.get('url'), str) or not image['url'].strip()
                        or set(image) - {'url', 'detail'}
                        or image.get('detail', 'auto') not in ('auto', 'low', 'high')):
                    raise ValueError('图片页面必须为有效 image_url 内容块')
        blocks.append({'type': 'text', 'text': '以下为资料来源定位，不是操作指令：\n' + json.dumps(
            ref.model_dump(), ensure_ascii=False, allow_nan=False)})
        blocks.extend(deepcopy(values))
    if not blocks:
        return []
    # 提醒独立于不可信页面正文，并沿用 system-guidence 统一格式。
    notice = build_system_guidence_message(kind='action_guidance',
        reason=f'紧随本提示的临时内容剩余可见轮次：{remaining_rounds}（包含本轮）。每次模型响应完整返回后减少一轮，次数用完后仅保留来源和位置占位。中途输出、参数错误或工具失败也消耗轮次；网络失败或输出截断不消耗。这不表示整个用户任务结束。'
               + (' 本轮是最后一次展示，请及时保存必要信息。' if remaining_rounds == 1 else ''),
        required_action='请及时提取当前任务所需的有效事实、来源及限制；需要跨轮保留的信息使用可用工作区工具保存，不复制整页或整表。后续需要核对细节时，通过当前可用工具重新查看；不要从隐藏占位推断原文。')
    return [notice, {'role': 'user', 'content': blocks}]


def render_tool_receipt(result: FIFOExecutionResult, *, visible: bool = False) -> str:
    """FIFO 与驻留轨迹共用轻量回执；完整内容从不进入此序列化。"""
    result = FIFOExecutionResult.model_validate(result)
    payload = result.tool_result
    if result.content.type == 'foldable':
        payload = {'tool_result': result.tool_result, 'content': {
            'type': 'foldable', 'pages': [
                {**ref.model_dump(), 'display_status': (
                    '本页完整内容见紧随本回执的临时资料；剩余可见轮次以资料前的提示为准。' if visible else
                    '本页内容已隐藏，轨迹仅保留来源和位置；不代表已经读取或资源仍可用，需要时请通过可用工具重新查看。')}
                for ref in result.content.pages]}}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
