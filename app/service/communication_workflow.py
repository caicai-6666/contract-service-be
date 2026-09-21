"""订阅激活后的正式门禁执行；对外只发布展示消息，不泄漏节点审计。"""

import asyncio
import html
import logging
import math
import json
import re
from collections.abc import Awaitable, Callable

from app.agent.contract_communication.business_gate import build_business_gate_subgraph
from app.agent.contract_communication.business_gate.subgraph.file_readability.state import ReadabilityFile
from app.agent.contract_communication.agent_core.context import assemble_agent_core_context
from app.schema.communication import ErrorData, MessageDeltaData, TaskProgressData, TurnStatusData
from app.service.communication import CommunicationEventService, CommunicationNotFoundError

from app.infrastructure.mllm import MLLMProviderNotReadyError, MODEL_PROVIDER_NOT_READY_MESSAGE

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
                 agent_core_runner: Callable[..., Awaitable[None]] | None = None, file_tools=None,
                 deepseek_settings=None, expert_session_capacity=10,
                 relation_cache_capacity=10, relation_page_size=5, relation_search_settings=None,
                 notes_cache_capacity=10, notes_page_size=5, **kwargs):
        super().__init__(**kwargs)
        if not math.isfinite(stream_delay) or stream_delay < 0:
            raise ValueError("流式间隔必须为有限非负数")
        self._gate_graph = gate_graph if gate_graph is not None else build_business_gate_subgraph(settings=settings)
        self._stream_delay = stream_delay
        self._workflow_tasks = {}
        # 后续执行器复用本轮队列和生命周期；只有完整门禁通过才调用。
        self._agent_core_runner = agent_core_runner
        self._file_tools = file_tools
        self._memory_viewers = {}
        self._relation_viewers = {}
        self._relation_search_pools = {}
        self._relation_search_settings = relation_search_settings
        self._notes_viewers = {}
        self._notes_cache_capacity = notes_cache_capacity
        self._notes_page_size = notes_page_size
        self._expert_session_capacity = expert_session_capacity
        self._relation_cache_capacity = relation_cache_capacity
        self._relation_page_size = relation_page_size
        self._relation_service = None
        self._contract_metadata = None
        self._agent_core_audits = {}
        self._development_traces = {}
        self._memory_search_audits = {}
        self._expert_settings = deepseek_settings
        self._expert_client = None
        self._expert_pools = {}
        self._expert_audits = {}

    def bind_contract_relations(self, relation_service, metadata_store):
        """启动期注入共享只读服务；分页与页面签名仍按会话隔离。"""
        self._relation_service = relation_service
        self._contract_metadata = metadata_store

    def _get_expert_pool(self, conversation_id):
        """仅从已授权的工具闭包调用；客户端共享，专家历史按宿主会话隔离。"""
        from app.core.config import get_settings
        from app.infrastructure.deepseek import DeepSeekClient, DeepSeekRequestError
        from app.agent.contract_communication.agent_core.tool.external_expert import ExternalExpertSessionPool
        if self._closed:
            raise DeepSeekRequestError('服务已关闭')
        pool = self._expert_pools.get(conversation_id)
        if pool is None:
            if self._expert_client is None:
                self._expert_client = DeepSeekClient(self._expert_settings or get_settings().deepseek)
            audit = self._expert_audits.setdefault(conversation_id, [])
            pool = ExternalExpertSessionPool(conversation_id, self._expert_client,
                capacity=self._expert_session_capacity, audit=audit.append)
            self._expert_pools[conversation_id] = pool
        return pool

    async def commit_agent_core_workspace(self, conversation_id, turn_id, *, owner, payload, expected_revision):
        from app.agent.contract_communication.agent_core.tool.workspace_executor import WorkspaceCommitRejected
        from app.infrastructure.communication_store import CommunicationStoreConflict
        async with self._condition:
            turn = self._get(conversation_id, turn_id, owner)
            if turn.snapshot.status != 'processing' or self._history is None or not turn.history_registered:
                raise WorkspaceCommitRejected('当前任务已失效')
            self._history.get_agent_core_snapshot_locked(conversation_id, turn_id)
            try:
                return self._history.update_workspace_locked(conversation_id, payload=payload, expected_revision=expected_revision)
            except CommunicationStoreConflict as exc:
                raise WorkspaceCommitRejected('工作区版本冲突') from exc

    async def commit_agent_core_summary(self, conversation_id, turn_id, *, owner, expected_summary, summary, scope):
        # 插入需要先拿备份锁，不能在持共享锁时等待，避免与后台 flush 死锁。
        async with self._condition:
            turn = self._get(conversation_id, turn_id, owner)
            if turn.snapshot.status != 'processing' or self._history is None or not turn.history_registered:
                raise ValueError('当前任务已失效')
        await self._history.insert_agent_summary(conversation_id, turn_id,
            expected_summary=expected_summary, summary=summary, scope=scope)

    async def get_agent_core_context(self, conversation_id, turn_id, *, owner, include_reasoning=True, reasoning_limits=None):
        """读取一致快照后在锁外渲染；每次读取都取得最新权威工作区。"""
        async with self._condition:
            turn = self._get(conversation_id, turn_id, owner)
            if turn.snapshot.status != 'processing' or self._history is None or not turn.history_registered:
                raise ValueError('Agent Core 需要仍在处理且已驻留的会话任务')
            workspace, summary, records = self._history.get_agent_core_snapshot_locked(conversation_id, turn_id)
            reasoning = self._history._resident[conversation_id].reasoning_window.model_copy(deep=True) if include_reasoning else None
            if reasoning is not None and reasoning_limits is not None:
                reasoning = reasoning.limited(max_rounds=reasoning_limits[0], max_tokens=reasoning_limits[1])
        return assemble_agent_core_context(workspace=workspace, summary=summary,
                                           records=records, current_turn_id=turn_id, reasoning_window=reasoning)

    async def record_agent_core_exchange(self, conversation_id, turn_id, *, owner, assistant_message, result):
        """在完整动作成功后记录真实调用及反馈；失败恢复由主循环私有管理。"""
        from app.agent.contract_communication.agent_core.subgraph.fifo_management.schema import FIFOExecutionResult
        from app.agent.contract_communication.agent_core.native_messages import validate_native_messages
        accepted = FIFOExecutionResult.model_validate(result)
        from app.agent.contract_communication.agent_core.context_rendering.page import render_tool_receipt
        if accepted.status != 'succeeded':
            raise ValueError('失败或不确定的调用不得写入已接受任务轨迹')
        calls = assistant_message.get('tool_calls', []) if isinstance(assistant_message, dict) else []
        if len(calls) != 1 or not isinstance(calls[0], dict):
            raise ValueError('必须提供真实的单个工具调用')
        messages = validate_native_messages([assistant_message,
            {'role': 'tool', 'tool_call_id': calls[0].get('id'),
             'content': render_tool_receipt(accepted)},
            *({**guidance.message, 'source': guidance.source} for guidance in accepted.system_guidence)])
        async with self._condition:
            turn = self._get(conversation_id, turn_id, owner)
            if turn.snapshot.status != 'processing' or self._history is None or not turn.history_registered:
                raise ValueError('已结束或未驻留任务不能记录原生交互')
            self._history.accept_agent_exchange_locked(conversation_id, turn_id, messages)

    async def record_agent_reasoning(self, conversation_id, turn_id, *, owner, **kwargs):
        async with self._condition:
            turn = self._get(conversation_id, turn_id, owner)
            if turn.snapshot.status != 'processing' or self._history is None or not turn.history_registered:
                raise ValueError('当前任务不能保存思考')
            await self._history.save_agent_reasoning_locked(conversation_id, turn_id, **kwargs)

    async def _prepare_agent_core(self, conversation_id, turn_id, owner, opened_files):
        async with self._condition:
            turn = self._get(conversation_id, turn_id, owner)
            if turn.snapshot.status != 'processing':
                raise ValueError('任务已结束，拒绝迟到的门禁结果')
            if self._history is None or not turn.history_registered:
                if self._agent_core_runner is not None:
                    raise ValueError('正式 Agent Core 必须绑定会话历史')
                return None  # 独立事件源测试没有会话存储，不伪造工作区。
            self._history.mark_agent_core_ready_locked(conversation_id, turn_id, opened_files)
        return await self.get_agent_core_context(conversation_id, turn_id, owner=owner)

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
        self._memory_viewers.pop(conversation_id, None)
        notes_viewer = self._notes_viewers.pop(conversation_id, None)
        if notes_viewer is not None:
            notes_viewer.close()
        search_pool = self._relation_search_pools.pop(conversation_id, None)
        if search_pool is not None:
            search_pool.close()
        viewer = self._relation_viewers.pop(conversation_id, None)
        if viewer is not None:
            viewer.close()
        pool = self._expert_pools.pop(conversation_id, None)
        if pool is not None:
            # 同步撤销提交资格，后续取消在途任务；迟到响应不能恢复已驱逐的历史。
            pool.close()
        self._expert_audits.pop(conversation_id, None)
        for key in tuple(self._development_traces):
            if key[0] == conversation_id:
                self._development_traces.pop(key)
        for key in tuple(self._memory_search_audits):
            if key[0] == conversation_id:
                self._memory_search_audits.pop(key)
        if self._file_tools is not None:
            self._file_tools.evict(conversation_id)
        for key in tuple(self._agent_core_audits):
            if key[0] == conversation_id:
                self._agent_core_audits.pop(key)
        for key, task in tuple(self._workflow_tasks.items()):
            if key[0] == conversation_id:
                task.cancel()

    def _prune(self):
        super()._prune()
        for key in tuple(self._development_traces):
            if key not in self._turns:
                self._development_traces.pop(key)
        for key in tuple(self._agent_core_audits):
            if key not in self._turns:
                self._agent_core_audits.pop(key)
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
        self._agent_core_audits.clear()
        self._development_traces.clear()
        self._memory_search_audits.clear()
        for pool in self._expert_pools.values():
            pool.close()
        self._expert_pools.clear()
        self._expert_audits.clear()
        if self._expert_client is not None:
            await self._expert_client.close()
            self._expert_client = None
        if self._file_tools is not None:
            await self._file_tools.close()
        self._memory_viewers.clear()
        for viewer in self._relation_viewers.values():
            viewer.close()
        self._relation_viewers.clear()
        for pool in self._relation_search_pools.values():
            pool.close()
        self._relation_search_pools.clear()
        for viewer in self._notes_viewers.values():
            viewer.close()
        self._notes_viewers.clear()

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
        from app.core.config import get_settings
        from app.infrastructure.development_trace import current_trace
        settings = get_settings()
        records = None
        if settings.communication_trace_enabled and settings.app_env == 'development':
            records = self._development_traces.setdefault((conversation_id, turn_id), [])
        token = current_trace.set(records)
        try:
            await self._run_observed(conversation_id, turn_id, owner)
        finally:
            current_trace.reset(token)

    async def _run_observed(self, conversation_id, turn_id, owner):
        after_gate_started = False
        try:
            await self.publish(conversation_id, turn_id, owner=owner,
                               data=TaskProgressData(type="thinking", message="正在思考"))
            source = await self.get_input(conversation_id, turn_id, owner=owner)
            files = tuple(ReadabilityFile(file_name=f.file_name, content=f.content)
                          for f in source.files) if source else ()
            history = await self.get_gate_history(conversation_id, turn_id, owner=owner)
            from app.infrastructure.development_trace import record
            record("context", "业务门禁开始")
            result = await self._gate_graph.ainvoke({
                "files": files, "text": source.text if source else None, "context": history,
                "contracts": source.contracts if source else ()})
            del history
            gate_status = result.get("status")
            record("event", "业务门禁结果", output={k: v for k, v in result.items()
                if k not in {"files", "context", "open_check", "render_check", "visual_check"}})
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
            if gate_status == 'passed':
                after_gate_started = True
                await self.resolve_file_admission(conversation_id, turn_id, owner=owner,
                                                 accepted_indices=tuple(range(len(files))))
                check = result.get('open_check')
                context = await self._prepare_agent_core(
                    conversation_id, turn_id, owner, check.opened_files if check is not None else ())
                # 上下文仅为本次派生快照；原文件与私有视觉产物不传入第二层。
                if self._agent_core_runner is not None:
                    del source, files, result
                    await self.enable_interruption(conversation_id, turn_id, owner=owner)
                    from app.agent.contract_communication.agent_core.tool.memory_viewer import MemoryQueryViewer, build_memory_view_registration
                    async with self._condition:
                        self._get(conversation_id, turn_id, owner)
                        secret_key = self._history._owners[conversation_id]
                    memory_pool = await self._history.memory_query_pool(conversation_id, secret_key=secret_key)
                    memory_viewer = self._memory_viewers.get(conversation_id)
                    if memory_viewer is None or memory_viewer.pool is not memory_pool:
                        memory_viewer = MemoryQueryViewer(memory_pool)
                        self._memory_viewers[conversation_id] = memory_viewer
                    from app.agent.contract_communication.agent_core.tool.memory_search import build_memory_search_registration
                    search_audit = self._memory_search_audits.setdefault((conversation_id, turn_id), [])
                    tools = [build_memory_view_registration(memory_viewer),
                             build_memory_search_registration(pool=memory_pool,
                                reference_time=context.tasks[-1].created_at, audit=search_audit)]
                    from app.agent.contract_communication.agent_core.tool.external_expert import build_external_expert_registrations
                    tools.extend(build_external_expert_registrations(
                        get_pool=lambda: self._get_expert_pool(conversation_id)))
                    notes_viewer = None
                    if self._contract_metadata is not None:
                        from app.agent.contract_communication.agent_core.tool.contract_metadata import build_contract_metadata_registration
                        tools.append(build_contract_metadata_registration(self._contract_metadata))
                        from app.agent.contract_communication.agent_core.tool.contract_statistics import build_contract_statistics_registration
                        tools.append(build_contract_statistics_registration(self._contract_metadata, self._relation_service))
                        from app.agent.contract_communication.agent_core.tool.contract_notes import ContractNotesViewer, build_contract_notes_registration
                        notes_viewer = self._notes_viewers.get(conversation_id)
                        if notes_viewer is None:
                            notes_viewer = ContractNotesViewer(metadata_store=self._contract_metadata,
                                max_resident=self._notes_cache_capacity, page_size=self._notes_page_size)
                            self._notes_viewers[conversation_id] = notes_viewer
                        tools.append(build_contract_notes_registration(notes_viewer))
                    relation_viewer = None
                    relation_search_pool = None
                    if self._relation_service is not None:
                        from app.agent.contract_communication.agent_core.tool.contract_relations import (
                            ContractRelationsViewer, build_contract_relations_registration)
                        # 任务已通过会话身份校验；同一驻留会话跨任务复用分页位置。
                        relation_viewer = self._relation_viewers.get(conversation_id)
                        if relation_viewer is None:
                            relation_viewer = ContractRelationsViewer(
                                relation_service=self._relation_service, metadata_store=self._contract_metadata,
                                max_resident=self._relation_cache_capacity, page_size=self._relation_page_size)
                            self._relation_viewers[conversation_id] = relation_viewer
                        tools.append(build_contract_relations_registration(relation_viewer))
                        from app.agent.contract_communication.agent_core.tool.contract_relation_search import (
                            ContractRelationSearchResults, build_contract_relation_search_registrations)
                        from app.core.config import get_settings
                        search_settings = self._relation_search_settings or get_settings()
                        relation_search_pool = self._relation_search_pools.get(conversation_id)
                        if relation_search_pool is None:
                            relation_search_pool = ContractRelationSearchResults(self._contract_metadata,self._relation_service,
                                capacity=search_settings.communication_relation_search_cache_max_queries,
                                page_size=search_settings.communication_relation_search_page_size)
                            self._relation_search_pools[conversation_id] = relation_search_pool
                        tools.extend(build_contract_relation_search_registrations(relation_search_pool,search_settings))
                    file_resolver = None
                    if self._file_tools is not None:
                        file_entries, file_resolver = await self._file_tools.prepare(conversation_id, secret_key)
                        tools.extend(file_entries)
                    async def resolve_page(reference):
                        if reference.resource_id.startswith('relation-search:') and relation_search_pool is not None:
                            return await relation_search_pool.resolve_page(reference)
                        if reference.resource_id.startswith('contract-notes:') and notes_viewer is not None:
                            return await notes_viewer.resolve_page(reference)
                        if reference.resource_id.startswith('contract-relations:') and relation_viewer is not None:
                            return await relation_viewer.resolve_page(reference)
                        if reference.resource_id.startswith('memory-query:'):
                            return await memory_viewer.resolve_page(reference)
                        if file_resolver is not None:
                            return await file_resolver(reference)
                        raise ValueError('页面引用无效，请重新调用查看工具。')
                    file_kwargs = dict(additional_tools=tools, page_resolver=resolve_page)
                    await self._agent_core_runner(self, conversation_id, turn_id, owner, context=context, **file_kwargs)
                    snapshot = await self.snapshot(conversation_id, turn_id, owner=owner)
                    if snapshot.status == 'processing':
                        raise ValueError('后续执行器返回时必须已结束本轮任务')
                    return
            # 仅兼容未配置后续执行器的独立测试或未完成的注入门禁。
            # 正式启动装配始终提供 Agent Core，不走此分支。
            has_files = bool(files)
            del source, files, result
            text = ("业务校验已通过，上下文已准备，但后续问答尚未接入，本轮暂不继续处理。"
                    if gate_status == 'passed' else
                    ("文件可读性检查与摘要生成已完成。" if has_files else "已收到你的问题。")
                    + "部分业务相关性判断与后续问答尚未接入，本轮暂不继续处理；上传文件不会保存。")
            await self._stream_final(conversation_id, turn_id, owner, text, "completed")
        except asyncio.CancelledError:
            raise
        except MLLMProviderNotReadyError:
            # 已取消或调整方向的任务不被迟到异常覆盖；服务故障不进入门禁拒绝反馈。
            snapshot = await self.snapshot(conversation_id, turn_id, owner=owner)
            if snapshot.status != "processing":
                return
            if not after_gate_started:
                await self.resolve_file_admission(conversation_id, turn_id, owner=owner, accepted_indices=())
            await self.publish(conversation_id, turn_id, owner=owner, data=ErrorData(
                code="model_provider_not_ready", message=MODEL_PROVIDER_NOT_READY_MESSAGE, retryable=True))
            if not snapshot.messages:
                await self._stream_final(conversation_id, turn_id, owner,
                                         MODEL_PROVIDER_NOT_READY_MESSAGE, "failed")
            else:
                await self.publish(conversation_id, turn_id, owner=owner, data=TurnStatusData(status="failed"))
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
