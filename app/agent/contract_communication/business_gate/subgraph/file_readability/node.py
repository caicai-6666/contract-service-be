"""文件可读性子图全部节点与路由：打开、渲染、视觉判断及汇总熔断。"""

import asyncio
from hashlib import sha256

from langgraph.graph import END
from langgraph.types import Send, Overwrite
from pydantic import TypeAdapter

from app.agent.contract_communication.business_gate.subgraph.file_readability.state import (
    FileReadabilityState, ReadabilityFile, OpenedFile, FileOpenFailure, FileOpenCheck,
    RenderedFile, FileRenderCheck, FileRenderFailure, FileVisualResult, FileVisualCheck,
    FileBranchState,
    FileOpenIssue, FileOpenFeedback,
    FileRenderIssue, FileRenderFeedback,
    FileVisualIssue, FileVisualFeedback,
)
from app.agent.contract_extraction.state import PreparedPDFPage
from app.core.config import MLLMSettings, get_settings
from app.infrastructure.mllm import MLLMClient, MLLMRequestError, MLLMUnavailableError
from .prompt.visual_readability import build_visual_readability_messages, VISUAL_READABILITY_PROMPT_VERSION
from .schema import judgment_json_schema, validate_judgment, JudgmentValidationError, build_validation_feedback
from app.tool.pdf_open import PDFOpenError, inspect_pdf_openable
from app.tool.pdf_page import PDFPageRenderConfig, PDFPageRenderError, compress_pdf_pages


_FILES = TypeAdapter(tuple[ReadabilityFile, ...])

# 前台模型消费的业务事实；现有 user_hints 继续沿用工具原因，不改变 SSE 文案。
_OPEN_FAILURE_HINTS = {
    'empty_file': '文件内容为空，无法打开或读取。',
    'not_pdf': '文件实际格式不是 PDF，无法按 PDF 读取；文件名以 .pdf 结尾不代表其实际格式为 PDF。',
    'password_required': '文件需要打开密码，当前无法读取其中的内容。',
    'no_pages': 'PDF 不包含任何页面，没有可供读取的页面内容。',
    'invalid_pdf': 'PDF 无法解析，可能存在文件损坏或格式无效的问题，具体原因尚不能确定。',
}


def _open_failure_feedback(*, file_index, file_name, code, file_count):
    """只记录已知的检查范围；不将可打开误写成分析完成或其他文件都正常。"""
    checked = (f'前{file_index}份文件仅确认可以打开' if file_index else '此前没有已通过打开检查的文件')
    remaining = file_count - file_index - 1
    unchecked = f'后{remaining}份文件尚未检查' if remaining else '没有尚未执行打开检查的后续文件'
    return FileOpenFeedback(
        hint=f'本轮上传{file_count}份文件。打开检查在第{file_index + 1}份文件处停止；'
             f'{checked}，{unchecked}。本轮尚未执行页面渲染及后续分析。',
        issues=(FileOpenIssue(file_index=file_index + 1, file_name=file_name,
                              hint=_OPEN_FAILURE_HINTS[code]),),
    )


def check_files_openable(state: FileReadabilityState) -> FileReadabilityState:
    """先校验内部输入契约，再按原顺序检查，首个失败立即熔断本轮。"""
    files = _FILES.validate_python(state.get('files', ()))
    reset = {'rendered_files': (), 'render_check': FileRenderCheck(status='not_started' if files else 'skipped')}
    opened = []
    for index, file in enumerate(files):
        try:
            page_count = inspect_pdf_openable(file.content)
        except PDFOpenError as exc:
            return {**reset, 'status': 'rejected', 'open_check': FileOpenCheck(
                status='rejected', opened_files=tuple(opened),
                failure=FileOpenFailure(file_index=index, file_name=file.file_name,
                                        code=exc.code, reason=exc.reason),
                feedback=_open_failure_feedback(file_index=index, file_name=file.file_name,
                                                code=exc.code, file_count=len(files)),
            )}
        opened.append(OpenedFile(file_index=index, file_name=file.file_name, page_count=page_count))
    # 每次重算，绝不复用调用方提供的结论；打开通过也不能伪造完整门禁通过。
    return {**reset, 'status': 'not_implemented', 'open_check': FileOpenCheck(
        status='passed' if files else 'skipped', opened_files=tuple(opened),
    )}


def route_after_open_check(state: FileReadabilityState) -> str:
    """没有文件或首个文件失败时，不调用渲染节点。"""
    return 'render' if state['open_check'].status == 'passed' else 'end'


def _render_failure_feedback(failures, *, collected=False):
    """只按稳定错误类别记录事实，不复制原生异常；汇总不遗漏后返回的问题文件。"""
    issues = []
    for failure in sorted(failures, key=lambda item: item.file_index):
        if failure.code == 'visual_budget_exceeded':
            hint = '文件页数超出当前可处理范围，未能完成页面渲染。'
        elif failure.code == 'resource_unavailable':
            hint = '服务器资源暂时不足或不可用，未能完成该文件的页面渲染；不能据此认定文件损坏。'
        elif failure.page_number is not None:
            hint = f'文件第{failure.page_number}页无法正常渲染，未能生成完整的页面图像，具体原因尚不能确定。'
        else:
            hint = '文件无法按当前页面处理规则完成渲染，未能生成完整的页面图像，具体原因及问题页码尚不能确定。'
        issues.append(FileRenderIssue(file_index=failure.file_index + 1,
                                      file_name=failure.file_name, hint=hint))
    scope = '本轮文件均已通过打开检查。以下文件未能完成页面渲染，因此未进行这些文件的视觉可读性判断。'
    if collected:
        scope += ('已等待所有文件分支结束；其他文件可能已经完成视觉判断，不能据此认定其全部正常。'
                  '本轮整体停止，尚未进入文件摘要及后续业务分析。')
    return FileRenderFeedback(hint=scope, issues=tuple(issues))


def check_files_renderable(
    state: FileReadabilityState, *, settings: MLLMSettings | None = None,
) -> FileReadabilityState:
    """复用提取流程的逐页压缩及动态预算，全部成功才提交内存页面对象。"""
    files = _FILES.validate_python(state.get('files', ()))
    opened = FileOpenCheck.model_validate(state['open_check'])
    if (opened.status != 'passed' or len(files) != len(opened.opened_files)
        or any(f.file_name != item.file_name for f, item in zip(files, opened.opened_files))):
        raise ValueError('渲染节点必须接收本轮全部通过的打开检查结果')
    settings = settings if settings is not None else get_settings().mllm
    rendered = []
    for index, (file, metadata) in enumerate(zip(files, opened.opened_files)):
        page_number = None
        try:
            # 与 AsyncPDFPreparationService 完全相同的按文件页数分配规则。
            per_page = settings.visual_token_budget_per_page(metadata.page_count)
            total_budget = settings.visual_token_budget(metadata.page_count)
        except ValueError:
            code, reason, status = 'visual_budget_exceeded', '文件页数超出当前视觉处理容量', 'rejected'
        else:
            try:
                config = PDFPageRenderConfig(
                    max_render_scale=settings.vision.max_render_scale,
                    visual_token_patch_size=settings.vision.visual_token_patch_size,
                    max_visual_tokens_per_page=per_page,
                )
                raw_pages = compress_pdf_pages(file.content, config=config, report_page_errors=True)
                pages = tuple(PreparedPDFPage(
                    page_number=p.page_number, png_bytes=p.png_bytes,
                    width_pixels=p.width_pixels, height_pixels=p.height_pixels,
                    width_points=p.width_points, height_points=p.height_points,
                    render_scale=p.render_scale, visual_tokens=p.visual_tokens,
                    content_sha256=sha256(p.png_bytes).hexdigest(),
                    was_scaled=p.render_scale < settings.vision.max_render_scale,
                ) for p in raw_pages)
                rendered.append(RenderedFile(
                    file_index=index, file_name=file.file_name, page_count=metadata.page_count,
                    total_visual_tokens=sum(p.visual_tokens for p in pages),
                    visual_tokens_per_page_budget=per_page, visual_tokens_per_request_budget=total_budget,
                    pages=pages,
                ))
                # 两种包装共享同一份 PNG bytes，不保留重复页面容器。
                del raw_pages, pages
                continue
            except PDFPageRenderError as exc:
                page_number = exc.page_number
                code, reason, status = 'render_failed', f'第 {page_number} 页无法正常渲染，本次处理已停止', 'rejected'
            except (MemoryError, OSError):
                code, reason, status = 'resource_unavailable', '服务器资源暂时不足或不可用，无法完成文件渲染，请稍后重试', 'failed'
            except (ValueError, RuntimeError, OverflowError):
                code, reason, status = 'render_failed', '文件无法按当前页面压缩规则完成渲染，本次处理已停止', 'rejected'
        # 不让前面成功文件的图像进入下游；局部引用随节点返回释放。
        failure = FileRenderFailure(file_index=index, file_name=file.file_name,
                                    page_number=page_number, code=code, reason=reason)
        return {'status': status, 'rendered_files': (), 'render_check': FileRenderCheck(
            status=status, failure=failure, feedback=_render_failure_feedback((failure,)),
        )}
    return {'status': 'not_implemented', 'rendered_files': tuple(rendered),
            'render_check': FileRenderCheck(status='passed')}


def _visual_failure_feedback(file, hint):
    """模型业务提示只能在完整校验后传入；单文件日志不提前宣称其他分支已经结束。"""
    return FileVisualFeedback(
        hint='该文件已完成页面渲染，但视觉检查发现不可读问题或未能取得有效结论。',
        issues=(FileVisualIssue(file_index=file.file_index + 1, file_name=file.file_name, hint=hint),),
    )


async def inspect_visual_readability(
    file: RenderedFile, *, settings: MLLMSettings,
    max_attempts: int = 3, client_factory=MLLMClient,
) -> FileVisualResult:
    """每份文件独立会话；默认一次请求加两次纠错，不共享失败历史。"""
    if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
        raise ValueError("视觉判断允许 1 至 3 次尝试，避免纠错历史超过预留容量")
    messages = build_visual_readability_messages(file)
    stable_length = len(messages)
    audit = []
    failure = "文件视觉校验未能完成，请稍后重试。"
    failure_hint = ('视觉检查' + ('多次' if max_attempts > 1 else '')
                    + '未能返回有效结果，暂时无法确认该文件是否可读；不能据此认定文件模糊或损坏。')
    try:
        async with client_factory(settings) as client:
            for attempt in range(1, max_attempts + 1):
                response = await client.create_json_chat_completion(
                    messages=messages, json_schema=judgment_json_schema(file.page_count),
                    max_completion_tokens=min(2048, settings.generation.max_completion_tokens),
                )
                record = {"attempt": attempt, "prompt_version": VISUAL_READABILITY_PROMPT_VERSION,
                          "response": response.raw_response, "accepted": False,
                          "message_count": len(messages)}
                audit.append(record)
                try:
                    if response.refusal or response.has_tool_calls:
                        raise JudgmentValidationError("本任务需要直接返回可读性 JSON，当前响应包含拒答或工具调用，不能作为判断。请依据页面提交完整 JSON，不调用工具。")
                    if response.finish_reason != "stop":
                        raise JudgmentValidationError("上一份输出没有正常结束，可能被长度限制截断，因此不能使用其中的结论。请缩短证据和理由，保留必要页码并重新提交完整 JSON。")
                    if not isinstance(response.content, str) or not response.content.strip():
                        raise JudgmentValidationError("上一份输出没有可使用的正文，因此没有证据与判断。请依据页面提交包含全部四个字段的完整 JSON。")
                    judgment = validate_judgment(response.content, page_count=file.page_count)
                except JudgmentValidationError as exc:
                    feedback = build_validation_feedback(str(exc))
                    record["feedback"] = feedback
                    if attempt < max_attempts:
                        # 需求方明确要求看见旧版完整输出：只在有限纠错期间保留。
                        messages.extend([{"role": "assistant", "content": response.content or ""}, feedback])
                    continue
                # 通过全部校验才能提交；清除整段失败链，审计不随之删除。
                del messages[stable_length:]
                record.update(accepted=True, message_count_after_cleanup=len(messages))
                return FileVisualResult(file_index=file.file_index, file_name=file.file_name,
                    status="passed" if judgment.result else "rejected", judgment=judgment,
                    feedback=None if judgment.result else _visual_failure_feedback(file, judgment.hint),
                    audit=tuple(audit))
            failure = "文件视觉检查多次返回无效结果，暂时无法完成校验，请稍后重试。"
    except (MLLMRequestError, MLLMUnavailableError) as exc:
        # 约束解码不受支持等 HTTP 错误不降级到无约束请求，也不指责文件损坏。
        audit.append({"error_type": type(exc).__name__, "accepted": False})
        failure = "模型服务暂时无法完成文件视觉校验，请稍后重试。"
        failure_hint = '视觉检查服务暂时无法完成该文件的检查，尚未取得可读性结论；不能据此认定文件有问题。'
    finally:
        # 包括取消路径：不保留可再次被误用的临时模型会话。
        del messages[stable_length:]
    return FileVisualResult(file_index=file.file_index, file_name=file.file_name,
        status="failed", error=failure, feedback=_visual_failure_feedback(file, failure_hint), audit=tuple(audit))


def open_files(state):
    update = check_files_openable(state)
    skipped = update['open_check'].status == 'skipped'
    failure = update['open_check'].failure
    return {**update, '_file_results': Overwrite(()), 'visual_results': (),
            'visual_check': FileVisualCheck(status='skipped' if skipped else 'not_started'),
            'user_hints': (f'文件「{failure.file_name}」：{failure.reason}',) if failure else ()}


def dispatch_files(state):
    if state['open_check'].status != 'passed':
        return END
    return [Send('check_file', {'file': ReadabilityFile.model_validate(file), 'metadata': metadata})
            for file, metadata in zip(state['files'], state['open_check'].opened_files, strict=True)]


def render_file(state, *, settings):
    """复用既有渲染规则；局部下标从0开始，完成后恢复原上传下标。"""
    metadata = state['metadata']
    local = check_files_renderable({
        'files': (state['file'],),
        'open_check': FileOpenCheck(status='passed', opened_files=(metadata.model_copy(update={'file_index': 0}),)),
    }, settings=settings)
    check = local['render_check']
    if check.failure:
        # 局部检查下标为0；恢复上传下标后再生成一基反馈，避免所有问题都指向第一份。
        failure = check.failure.model_copy(update={'file_index': metadata.file_index})
        check = FileRenderCheck(status=check.status, failure=failure,
                                feedback=_render_failure_feedback((failure,)))
    rendered = (local['rendered_files'][0].model_copy(update={'file_index': metadata.file_index})
                if check.status == 'passed' else None)
    return {'render_check': check, 'rendered': rendered}


def finish_file(state):
    metadata = state['metadata']
    visual = state.get('visual') or FileVisualResult(
        file_index=metadata.file_index, file_name=metadata.file_name, status='skipped')
    return {'_file_results': ({'metadata': metadata, 'render_check': state['render_check'],
                              'rendered': state.get('rendered'), 'visual': visual},)}


def collect_file_results(state):
    """按上传顺序确定结果，不让先返回的分支覆盖其他分支；缺失不得放行。"""
    rows = sorted(state['_file_results'], key=lambda row: row['metadata'].file_index)
    expected = state['open_check'].opened_files
    if [row['metadata'] for row in rows] != list(expected):
        raise ValueError('并发结果必须唯一、完整地覆盖本轮上传文件')
    render_errors = [r['render_check'] for r in rows if r['render_check'].status != 'passed']
    visuals = tuple(row['visual'] for row in rows)
    failures = [v for v in visuals if v.status == 'failed']
    rejected = [v for v in visuals if v.status == 'rejected']
    # 技术失败优先，避免同时存在不可读文件时掩盖服务故障。
    status = ('failed' if failures or any(c.status == 'failed' for c in render_errors)
              else 'rejected' if render_errors or rejected else 'not_implemented')
    chosen = next((c for c in render_errors if c.status == 'failed'), None)
    render_check = chosen or (render_errors[0] if render_errors else FileRenderCheck(status='passed'))
    if render_errors:
        # failure 保持既有代表性错误语义；feedback 独立保留每份失败文件，并按上传顺序整理。
        render_check = FileRenderCheck(status=render_check.status, failure=render_check.failure,
            feedback=_render_failure_feedback(tuple(check.failure for check in render_errors), collected=True))
    visual_status = ('failed' if failures else 'rejected' if rejected
                     else 'skipped' if render_errors else 'passed')
    # 仅汇总节点3的正式问题；渲染失败的 skipped 分支由节点2记录，不重复归因。
    visual_feedback = None
    if failures or rejected:
        visual_feedback = FileVisualFeedback(
            hint='以下文件已完成页面渲染，但视觉检查发现不可读问题或未能取得有效结论。'
                 '本轮整体停止，尚未进入文件摘要及后续业务分析。',
            issues=tuple(issue for visual in visuals if visual.status in {'rejected', 'failed'}
                         for issue in visual.feedback.issues),
        )
    hints = []
    for row in rows:
        name, check, visual = row['metadata'].file_name, row['render_check'], row['visual']
        reason = (check.failure.reason if check.failure else visual.error if visual.status == 'failed'
                  else visual.judgment.hint if visual.status == 'rejected' else None)
        if reason:
            hints.append(f'文件「{name}」：{reason}')
    return {'status': status, 'render_check': render_check,
            'visual_check': FileVisualCheck(status=visual_status, feedback=visual_feedback), 'visual_results': visuals,
            'user_hints': tuple(hints),
            # 整轮失败不交付部分页面；清空 reducer，避免内部残留半成品 PNG。
            'rendered_files': tuple(row['rendered'] for row in rows) if status == 'not_implemented' else (),
            '_file_results': Overwrite(())}


async def check_file_visual_readability(
    state: FileBranchState, *, settings: MLLMSettings | None = None,
    max_attempts: int = 3, client_factory=MLLMClient,
) -> FileBranchState:
    """单文件视觉节点入口；每个 Map 分支独立执行模型会话。"""
    return {'visual': await inspect_visual_readability(
        state['rendered'], settings=settings or get_settings().mllm,
        max_attempts=max_attempts, client_factory=client_factory)}


def check_file_visual_readability_sync(state: FileBranchState, **kwargs) -> FileBranchState:
    """为脚本的同步 invoke 提供适配；服务端仍使用原生异步入口。"""
    return asyncio.run(check_file_visual_readability(state, **kwargs))


def route_after_render_check(state: FileBranchState) -> str:
    """渲染失败时跳过视觉判断，直接收集当前文件的失败结果。"""
    return 'visual' if state['render_check'].status == 'passed' else 'finish'


__all__ = [
    'check_files_openable', 'check_files_renderable', 'route_after_open_check',
    'inspect_visual_readability', 'open_files', 'dispatch_files', 'render_file',
    'finish_file', 'collect_file_results', 'check_file_visual_readability',
    'check_file_visual_readability_sync', 'route_after_render_check',
]
