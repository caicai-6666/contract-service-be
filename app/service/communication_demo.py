"""Communication 展示工作流：确定性模拟事件，不执行真实合同分析。"""

import asyncio
import logging
import math
import re

from app.schema.communication import (
    ErrorData, MessageCompletedData,
    MessageDeltaData, TaskProgressData, TurnStatusData,
)
from app.service.communication import CommunicationEventService, CommunicationNotFoundError
from app.service.communication_workflow import format_gate_notice

SCENARIOS = ("success", "online", "clarify", "rejected", "failure", "recoverable", "slow", "heartbeat", "replay")
logger = logging.getLogger(__name__)

# 固定虚构样本贯穿检索、计算、对比和总结，避免各阶段数据相互矛盾。
# 工具调用保留在内部轨迹；对外进度按业务阶段归类，不逐工具播报。
DEMO_STEPS = (
    (
        "search_contracts", "检索合同", "关键词：设备采购；类别：采购合同；候选数：2",
        "检索到 2 份虚构样本",
        "**检索结果**\n\n"
        "- 样本 A：光伏组件采购合同，负责人：模拟审核员甲。\n"
        "- 样本 B：设备采购合同，负责人：模拟审核员乙。\n\n"
        "接下来核对付款节点和验收约定；这些样本不对应库内真实合同。",
    ),
    (
        "read_contract_clauses", "读取关键条款", "对象：样本 A、B；范围：付款、验收、质保",
        "完成 3 类条款的模拟读取",
        "**条款摘要**\n\n"
        "样本 A 总额 120 万元，付款比例为 30% / 60% / 10%；"
        "样本 B 为 20% / 70% / 10%。两者尾款均为 10%。\n\n"
        "样本 A 未明确验收期限，样本 B 约定到货后 10 个工作日验收。下一步计算付款差额。",
    ),
    (
        "calculate_payment_schedule", "计算付款计划", "统一比较基数：120 万元；A：30/60/10；B：20/70/10",
        "两组比例均合计 100%，预付款差额 12 万元",
        "**计算结果**（统一按 120 万元测算）\n\n"
        "| 节点 | 样本 A | 样本 B |\n| --- | --- | --- |\n"
        "| 预付款 | 36 万元 | 24 万元 |\n| 到货款 | 72 万元 | 84 万元 |\n"
        "| 尾款 | 12 万元 | 12 万元 |\n\n"
        "A 的预付款比 B 多 12 万元。下一步结合验收条件展示差异。",
    ),
    (
        "compare_contract_terms", "对比条款", "维度：预付款比例、验收期限、尾款条件",
        "整理出 2 项差异、1 项待确认信息",
        "**对比结果**\n\n"
        "1. A 的预付款比例比 B 高 10 个百分点。\n"
        "2. A 缺少明确验收期限，B 为 10 个工作日。\n"
        "3. 两份样本均需补充核对尾款释放条件。\n\n"
        "资料尚不足以判断实际合同风险，下面只汇总本次虚构样本的核对建议。",
    ),
)


class DemoEventService(CommunicationEventService):
    """供独立脚本使用的纯模拟生产者；遵循正式事件生命周期。"""

    def __init__(self, *, scenario="success", delay=0.2, slow_seconds=60, tool_seconds=4, **kwargs):
        super().__init__(**kwargs)
        if scenario not in SCENARIOS:
            raise ValueError("未知模拟场景")
        if any(not math.isfinite(value) or value <= 0 for value in (delay, slow_seconds, tool_seconds)):
            raise ValueError("模拟间隔必须为有限正数")
        self.scenario = scenario
        self.delay = delay
        self.slow_seconds = slow_seconds
        self.tool_seconds = tool_seconds
        self._demo_tasks = {}

    async def subscribe(self, conversation_id, turn_id, *, owner, after_sequence=0):
        stream = await super().subscribe(conversation_id, turn_id, owner=owner, after_sequence=after_sequence)
        snapshot = await self.snapshot(conversation_id, turn_id, owner=owner)
        key = (conversation_id, turn_id)
        # 检查与创建之间没有 await；并发订阅只启动一个模拟生产者。
        if snapshot.status == "processing" and key not in self._demo_tasks:
            task = asyncio.create_task(self._run(conversation_id, turn_id, owner))
            self._demo_tasks[key] = task
            task.add_done_callback(lambda finished: self._demo_tasks.pop(key, None))
        return stream

    async def register_turn(self, conversation_id, turn_id, *, owner, **kwargs):
        snapshot = await super().register_turn(conversation_id, turn_id, owner=owner, **kwargs)
        old_id = kwargs.get("supersedes_turn_id")
        if old_id is not None:
            await self._stop((conversation_id, old_id))
        return snapshot

    async def cancel_turn(self, conversation_id, turn_id, *, owner):
        snapshot = await super().cancel_turn(conversation_id, turn_id, owner=owner)
        await self._stop((conversation_id, turn_id))
        return snapshot

    def _evict_conversation_locked(self, conversation_id):
        super()._evict_conversation_locked(conversation_id)
        for key, task in tuple(self._demo_tasks.items()):
            if key[0] == conversation_id:
                task.cancel()

    async def _stop(self, key):
        task = self._demo_tasks.get(key)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def close(self):
        tasks = list(self._demo_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._demo_tasks.clear()
        await super().close()

    async def _run(self, conversation_id, turn_id, owner):
        await run_demo_workflow(self, conversation_id, turn_id, owner, scenario=self.scenario,
                                delay=self.delay, slow_seconds=self.slow_seconds, tool_seconds=self.tool_seconds)


async def run_demo_workflow(service: CommunicationEventService, conversation_id, turn_id, owner, *,
                            scenario="success", delay=0.2, slow_seconds=60, tool_seconds=4,
                            after_gate=False):
    """在调用方的同一轮事件队列输出模拟内容，不创建另一个运行时或后台生产者。

    after_gate 只由应用装配指定；为 true 时附件准入已由真实门禁提交，脚本不得改写。
    独立 DemoEventService 保留纯模拟行为，不请求模型。
    """
    async def emit(data):
        await service.publish(conversation_id, turn_id, owner=owner, data=data)

    async def message(message_id, kind, text, *, interleave=False):
        for index in range(0, len(text), 4):
            await emit(MessageDeltaData(message_id=message_id, message_kind=kind, delta=text[index:index + 4]))
            if interleave and index == 8:
                await emit(TaskProgressData(type="local-search", message="正在查阅相关合同的付款与验收条款"))
            await asyncio.sleep(delay)
        await emit(MessageCompletedData(message_id=message_id, message_kind=kind, text=text))

    try:
        source = await service.get_input(conversation_id, turn_id, owner=owner)
        text = (source.text or "") if source is not None else ""
        marker = re.match(r"\s*\[demo:([a-z]+)\]", text)
        scenario = marker.group(1) if marker else scenario
        if scenario not in SCENARIOS:
            raise ValueError("未知模拟场景")
        # 门禁后的模拟层不能推翻已接受的准入结论；此场景仅保留在独立脚本。
        if after_gate and scenario == "rejected":
            scenario = "success"
        logger.info("联调轮次激活：scenario=%s turn_id=%s after_gate=%s", scenario, turn_id, after_gate)
        await emit(TaskProgressData(type="thinking", message="正在理解你的问题"))
        await asyncio.sleep(delay)
        names = [file.file_name for file in source.files] if source else []
        # 门禁是内部步骤：进度只描述动作，检查结论通过消息进入快照与历史。
        await emit(TaskProgressData(
            type="thinking", message="正在思考",
        ))
        await asyncio.sleep(delay)
        if scenario == "rejected":
            await service.resolve_file_admission(conversation_id, turn_id, owner=owner, accepted_indices=())
            notice = format_gate_notice(("该问题与合同业务无关，请重新提出合同相关问题。",), rejected=True)
            for index in range(0, len(notice), 4):
                await emit(MessageDeltaData(message_id="final-1", message_kind="final", delta=notice[index:index + 4]))
                await asyncio.sleep(delay)
            await service.finish_with_message(conversation_id, turn_id, owner=owner,
                                           message_id="final-1", text=notice, status="rejected")
            return
        if after_gate and scenario == "clarify":
            await message("final-1", "final", "你希望优先核对付款安排、验收条件，还是质保责任？请在下一轮告诉我。")
            await emit(TurnStatusData(status="completed"))
            return
        if scenario == "clarify":
            await service.resolve_file_admission(
                conversation_id, turn_id, owner=owner, accepted_indices=tuple(range(1, len(names))),
            )
            if names:
                results = [f"未保留：{names[0]}，原因：文件无法读取。"]
                results.extend(f"已保留：{name}，文件检查通过。" for name in names[1:])
                await message("notice-1", "intermediate", "文件检查结果：\n\n" + "\n".join(results))
            clarification = (
                "部分文件未通过检查。是否使用保留的文件继续？请在下一轮回复。"
                if len(names) > 1 else
                "请在下一轮上传可读取的 PDF 后继续。"
            )
            await message("final-1", "final", clarification)
            await emit(TurnStatusData(status="completed"))
            return
        if not after_gate:
            # 独立脚本才模拟准入；混合模式由真实门禁提交一次，不重复覆盖附件状态。
            await service.resolve_file_admission(
                conversation_id, turn_id, owner=owner, accepted_indices=tuple(range(len(names))),
            )
        if scenario == "failure":
            await emit(MessageDeltaData(message_id="notice-2", message_kind="intermediate", delta="正在生成的半段提示……"))
            await asyncio.sleep(delay)
            await emit(ErrorData(code="demo_analysis_failed", message="分析服务不可用", retryable=False))
            await emit(TurnStatusData(status="failed"))
            return
        if scenario == "recoverable":
            await emit(ErrorData(code="demo_lookup_retry", message="首次查询失败，正在尝试其他方式", retryable=True))
            await asyncio.sleep(delay * 3)
            await emit(TaskProgressData(type="thinking", message="查询已恢复，继续分析"))
        if scenario == "heartbeat":
            await asyncio.sleep(service.heartbeat_seconds * 2.5)
        if scenario == "slow":
            await message("notice-2", "intermediate", "这一步需要一些时间。你可以随时停止，也可以补充要求或调整处理方向。")
            await emit(TaskProgressData(type="thinking", message="正在分析合同中的关键约定"))
            await asyncio.sleep(slow_seconds)
        if scenario == "replay":
            # 超过缓存长度，便于前端使用旧游标验证 409 与快照恢复。
            for step in range(service._buffer_size + 20):
                await emit(TaskProgressData(type="thinking", message=f"回放压力事件 {step + 1}"))
                await asyncio.sleep(min(delay, 0.01))
        for step, (tool, title, arguments, result, explanation) in enumerate(DEMO_STEPS, 1):
            # 两个查阅工具共用本地查阅状态，计算与对比共用思考状态。
            if step == 1:
                await emit(TaskProgressData(type="local-search", message="正在查阅相关采购合同"))
            elif step == 3:
                await emit(TaskProgressData(type="thinking", message="正在分析付款安排与验收约定"))
            call_id = f"demo-call-{step:03d}"
            # 函数名与调用编号用于内部轨迹和日志定位，不拼入公开进度。
            logger.info("展示工具开始：turn_id=%s call_id=%s tool=%s", turn_id, call_id, tool)
            await service.record_tool_call(conversation_id, turn_id, owner=owner, call_id=call_id,
                                        name=tool, title=title, input_summary=arguments)
            await asyncio.sleep(tool_seconds)
            logger.info("展示工具完成：turn_id=%s call_id=%s tool=%s", turn_id, call_id, tool)
            await service.record_tool_result(conversation_id, turn_id, owner=owner, call_id=call_id,
                                          status='succeeded', output_summary=f"{result}\n\n{explanation}")
            # 仅交付有价值的阶段结果，不把调用过程或内部推理转写成消息。
            if step in (1, 4):
                await message(f"step-{step}-result", "intermediate", explanation, interleave=step == 1)
        if scenario == "online":
            await emit(TaskProgressData(type="online-search", message="正在联网检索验收条款相关资料"))
            await service.record_tool_call(conversation_id, turn_id, owner=owner, call_id="demo-call-online",
                                        name="search_web", title="联网检索", input_summary="验收期限与异议通知")
            await asyncio.sleep(tool_seconds)
            await service.record_tool_result(conversation_id, turn_id, owner=owner, call_id="demo-call-online",
                                          status="succeeded", output_summary="展示流程结束，未发起真实网络请求或获取网页资料")
            await message("online-result", "intermediate", "本次未提供可核验的网页来源，以下结论仅依据已展示的合同样本。")
        await emit(TaskProgressData(type="thinking", message="正在整理分析结论"))
        await message(
            "final-1", "final",
            "## 合同对比小结\n\n"
            "已完成 2 份虚构采购合同的检索、条款读取、付款计算与对比。\n\n"
            "**建议优先核对**\n\n"
            "- 预付款：按 120 万元测算，A 为 36 万元，B 为 24 万元，相差 12 万元。\n"
            "- 验收：为 A 补充明确的验收期限、标准与异议处理方式。\n"
            "- 尾款：两份样本均需确认释放条件和质保衔接。\n\n"
            "下一步可以继续查看付款计划，或整理验收条款核对清单。",
        )
        await emit(TurnStatusData(status="completed"))
    except asyncio.CancelledError:
        raise
    except Exception:
        # 旧轮次已终止时不再补写事件；其他模拟异常显式失败，避免静默挂起。
        try:
            state = await service.snapshot(conversation_id, turn_id, owner=owner)
            if state.status == "processing":
                logger.exception("模拟执行失败：turn_id=%s", turn_id)
                await emit(ErrorData(code="demo_driver_error", message="联调脚本执行失败，请检查场景标记和服务日志", retryable=False))
                await emit(TurnStatusData(status="failed"))
        except (CommunicationNotFoundError, ValueError):
            pass
