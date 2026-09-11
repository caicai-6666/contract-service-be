"""订阅激活后的正式门禁执行；对外只发布展示消息，不泄漏节点审计。"""

import asyncio
import html
import logging
import math
import re
from collections.abc import Awaitable, Callable

from app.agent.contract_communication.business_gate import build_business_gate_subgraph
from app.agent.contract_communication.business_gate.subgraph.file_readability.state import ReadabilityFile
from app.schema.communication import ErrorData, MessageDeltaData, TaskProgressData, TurnStatusData
from app.service.communication import CommunicationEventService, CommunicationNotFoundError

logger = logging.getLogger(__name__)


def format_gate_notice(hints, *, rejected: bool) -> str:
    """固定标题、原因列表和下一步；外部文件名与模型提示只作为纯文本展示。

    限制总展示长度，避免批量文件的提示超过单事件额度；不拼接私有异常或推理。
    """
    title = "本次请求未通过校验" if rejected else "暂时无法完成校验"
    fallback = "本次输入未通过校验，请检查文件后重新提交。" if rejected else "校验服务暂时不可用，请稍后重试。"
    reasons = [hint for hint in hints if isinstance(hint, str) and hint.strip()]
    lines = []
    used = 0
    for hint in reasons or [fallback]:
        clean = html.escape(" ".join(hint.split()), quote=False)
        clean = re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", clean)
        if len(clean) > 1200:
            clean = clean[:1200] + "…（提示过长，已截断）"
        if used + len(clean) > 5000:
            lines.append("- 另有其他校验问题，请检查其余文件。")
            break
        lines.append("- " + clean)
        used += len(clean)
    next_step = "请根据上述提示调整后重新提交。" if rejected else "请稍后重新提交；本次未继续处理。"
    return f"## {title}\n\n" + "\n".join(lines) + f"\n\n{next_step}"


class CommunicationWorkflowService(CommunicationEventService):
    """每轮只启动一个生产者；连接断开不取消任务，显式取消/替代/删除会停止执行。"""

    requires_business_gate = True

    def __init__(self, *, gate_graph=None, settings=None, stream_delay=0.01,
                 after_gate: Callable[..., Awaitable[None]] | None = None, **kwargs):
        super().__init__(**kwargs)
        if not math.isfinite(stream_delay) or stream_delay < 0:
            raise ValueError("流式间隔必须为有限非负数")
        self._gate_graph = gate_graph if gate_graph is not None else build_business_gate_subgraph(settings=settings)
        self._stream_delay = stream_delay
        self._workflow_tasks = {}
        # 后续执行器复用本轮队列和生命周期；只有完整门禁通过才调用。
        self._after_gate = after_gate

    async def subscribe(self, conversation_id, turn_id, *, owner, after_sequence=0):
        stream = await super().subscribe(conversation_id, turn_id, owner=owner, after_sequence=after_sequence)
        # 与取消、关闭共用锁，避免读到旧 processing 快照后又启动已结束任务。
        async with self._condition:
            turn = self._get(conversation_id, turn_id, owner)
            key = (conversation_id, turn_id)
            if turn.snapshot.status == "processing" and key not in self._workflow_tasks:
                task = asyncio.create_task(self._run(conversation_id, turn_id, owner))
                self._workflow_tasks[key] = task
                def release(finished):
                    # 清理旧协程时，不误删同一标识后来注册的新生产者。
                    if self._workflow_tasks.get(key) is finished:
                        self._workflow_tasks.pop(key, None)
                task.add_done_callback(release)
        return stream

    async def register_turn(self, conversation_id, turn_id, *, owner, **kwargs):
        snapshot = await super().register_turn(conversation_id, turn_id, owner=owner, **kwargs)
        if kwargs.get("supersedes_turn_id") is not None:
            await self._stop((conversation_id, kwargs["supersedes_turn_id"]))
        return snapshot

    async def cancel_turn(self, conversation_id, turn_id, *, owner):
        snapshot = await super().cancel_turn(conversation_id, turn_id, owner=owner)
        await self._stop((conversation_id, turn_id))
        return snapshot

    async def _stop(self, key):
        task = self._workflow_tasks.get(key)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _evict_conversation_locked(self, conversation_id):
        super()._evict_conversation_locked(conversation_id)
        for key, task in tuple(self._workflow_tasks.items()):
            if key[0] == conversation_id:
                task.cancel()

    def _prune(self):
        super()._prune()
        # TTL 到期不仅冻结事件，也取消仍在等待模型的生产者。
        for key, task in tuple(self._workflow_tasks.items()):
            if key not in self._turns:
                task.cancel()

    async def close(self):
        # 先封闭生命周期，防止关闭等待期间新的订阅又启动生产者。
        await super().close()
        tasks = tuple(self._workflow_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._workflow_tasks.clear()

    async def _stream_final(self, conversation_id, turn_id, owner, text, status):
        message_id = "gate-final"
        # 完整回复已校验或由程序兜底后才分块展示，不直接透传模型 JSON。
        for offset in range(0, len(text), 24):
            await self.publish(conversation_id, turn_id, owner=owner, data=MessageDeltaData(
                message_id=message_id, message_kind="final", delta=text[offset:offset + 24]))
            await asyncio.sleep(self._stream_delay)
        await self.finish_with_message(conversation_id, turn_id, owner=owner,
                                       message_id=message_id, text=text, status=status)

    async def _run(self, conversation_id, turn_id, owner):
        after_gate_started = False
        try:
            await self.publish(conversation_id, turn_id, owner=owner,
                               data=TaskProgressData(type="thinking", message="正在思考"))
            source = await self.get_input(conversation_id, turn_id, owner=owner)
            files = tuple(ReadabilityFile(file_name=f.file_name, content=f.content)
                          for f in source.files) if source else ()
            history = await self.get_gate_history(conversation_id, turn_id, owner=owner)
            result = await self._gate_graph.ainvoke({
                "files": files, "text": source.text if source else None, "context": history})
            del history
            gate_status = result.get("status")
            if gate_status not in {"rejected", "failed", "not_implemented", "passed"}:
                raise ValueError("门禁返回了未约定的状态")
            # 只采纳完整摘要阶段的轻量产物，先关联原始附件，再处理准入与终态冻结。
            # 后续相关性拒绝不抹掉已生成的描述，但仍禁止文件落盘及二层上下文准入。
            if result.get('file_summary_status') == 'completed':
                await self.record_file_summaries(conversation_id, turn_id, owner=owner,
                                                 summaries=result.get('file_summaries', ()))
            if gate_status in {"rejected", "failed"}:
                reply = result.get('rejection_reply')
                if reply is not None:
                    # 模型回复按纯文本转义后分块输出，不透传 HTML、链接或内部审计。
                    text = html.escape(reply.message, quote=False)
                    text = re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", text)
                else:
                    # 兼容旧的注入执行器；正式图的未放行结果均经过 reject_request。
                    text = format_gate_notice(result.get("user_hints", ()), rejected=gate_status == "rejected")
                await self.resolve_file_admission(conversation_id, turn_id, owner=owner, accepted_indices=())
                if gate_status == "failed":
                    await self.publish(conversation_id, turn_id, owner=owner, data=ErrorData(
                        code="business_gate_failed", message="校验暂时无法完成，请查看本轮提示。", retryable=False))
                # 拒绝后尽早释放本执行器持有的文件引用；附件元数据仍保留在用户历史。
                del source, files, result
                await self._stream_final(conversation_id, turn_id, owner, text, gate_status)
                return
            if gate_status == 'passed' and self._after_gate is not None:
                await self.resolve_file_admission(conversation_id, turn_id, owner=owner,
                                                 accepted_indices=tuple(range(len(files))))
                # 当前 mock 不读取页面；释放本地视觉结果，不复制原文件给另一个运行时。
                del source, files, result
                await self.enable_interruption(conversation_id, turn_id, owner=owner)
                after_gate_started = True
                await self._after_gate(self, conversation_id, turn_id, owner)
                snapshot = await self.snapshot(conversation_id, turn_id, owner=owner)
                if snapshot.status == 'processing':
                    raise ValueError('后续执行器返回时必须已结束本轮任务')
                return
            # 四个相关性维度与聚合均已接入，但核心问答仍未实现。
            # 这里不批准附件落盘，也不生成虚假的合同分析结论。
            has_files = bool(files)
            del source, files, result
            text = ("业务校验已通过，但后续问答尚未接入，本轮暂不继续处理；上传文件不会保存。"
                    if gate_status == 'passed' else
                    ("文件可读性检查与摘要生成已完成。" if has_files else "已收到你的问题。")
                    + "部分业务相关性判断与后续问答尚未接入，本轮暂不继续处理；上传文件不会保存。")
            await self._stream_final(conversation_id, turn_id, owner, text, "completed")
        except asyncio.CancelledError:
            raise
        except Exception:
            # 迟到结果不覆盖用户取消或方向调整；异常详情只进入服务端日志。
            try:
                snapshot = await self.snapshot(conversation_id, turn_id, owner=owner)
                if snapshot.status != "processing":
                    return
                logger.exception("Communication 工作流执行失败：turn_id=%s", turn_id)
                if not snapshot.messages:
                    if after_gate_started:
                        # 后续执行器故障不是门禁失败，不能误导用户重新修复已通过的文件。
                        await self.publish(conversation_id, turn_id, owner=owner, data=ErrorData(
                            code='communication_execution_failed', message='后续处理暂时无法完成，请稍后重试。', retryable=False))
                        await self._stream_final(conversation_id, turn_id, owner,
                            '## 暂时无法完成处理\n\n后续问答执行失败，请稍后重试。', 'failed')
                    else:
                        await self._stream_final(conversation_id, turn_id, owner,
                                                 format_gate_notice((), rejected=False), "failed")
                else:
                    # 输出过程中发生容量等故障时，保留半成品并可靠结束，不再追加第二条 final。
                    await self.publish(conversation_id, turn_id, owner=owner, data=TurnStatusData(status="failed"))
            except (CommunicationNotFoundError, ValueError):
                pass
