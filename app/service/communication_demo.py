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

SCENARIOS = ("success", "clarify", "rejected", "failure", "recoverable", "slow", "heartbeat", "replay")
logger = logging.getLogger(__name__)

# 固定虚构样本贯穿检索、计算、对比和总结，避免各阶段数据相互矛盾。
# 工具调用暂用现有进度事件展示，不引入未经约定的新 SSE 类型或真实工具副作用。
DEMO_STEPS = (
    (
        "search_contracts", "检索合同", "关键词：设备采购；类别：采购合同；候选数：2",
        "正在按类别筛选，并匹配合同标题",
        "检索到 2 份虚构样本",
        "**检索结果**\n\n"
        "- 样本 A：光伏组件采购合同，负责人：模拟审核员甲。\n"
        "- 样本 B：设备采购合同，负责人：模拟审核员乙。\n\n"
        "接下来核对付款节点和验收约定；这些样本不对应库内真实合同。",
    ),
    (
        "read_contract_clauses", "读取关键条款", "对象：样本 A、B；范围：付款、验收、质保",
        "正在整理两份样本的条款摘要",
        "完成 3 类条款的模拟读取",
        "**条款摘要**\n\n"
        "样本 A 总额 120 万元，付款比例为 30% / 60% / 10%；"
        "样本 B 为 20% / 70% / 10%。两者尾款均为 10%。\n\n"
        "样本 A 未明确验收期限，样本 B 约定到货后 10 个工作日验收。下一步计算付款差额。",
    ),
    (
        "calculate_payment_schedule", "计算付款计划", "统一比较基数：120 万元；A：30/60/10；B：20/70/10",
        "正在校验比例合计，并计算三个付款节点",
        "两组比例均合计 100%，预付款差额 12 万元",
        "**计算结果**（统一按 120 万元测算）\n\n"
        "| 节点 | 样本 A | 样本 B |\n| --- | --- | --- |\n"
        "| 预付款 | 36 万元 | 24 万元 |\n| 到货款 | 72 万元 | 84 万元 |\n"
        "| 尾款 | 12 万元 | 12 万元 |\n\n"
        "A 的预付款比 B 多 12 万元。下一步结合验收条件展示差异。",
    ),
    (
        "compare_contract_terms", "对比条款", "维度：预付款比例、验收期限、尾款条件",
        "正在汇总差异，并标记缺失信息",
        "整理出 2 项差异、1 项待确认信息",
        "**对比结果**\n\n"
        "1. A 的预付款比例比 B 高 10 个百分点。\n"
        "2. A 缺少明确验收期限，B 为 10 个工作日。\n"
        "3. 两份样本均需补充核对尾款释放条件。\n\n"
        "资料尚不足以判断实际合同风险，下面只汇总本次虚构样本的核对建议。",
    ),
)


class DemoEventService(CommunicationEventService):
    """供现有后端及独立脚本复用的展示生产者；遵循正式事件生命周期。"""

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
        async def emit(data):
            await self.publish(conversation_id, turn_id, owner=owner, data=data)

        async def message(message_id, kind, text, *, interleave=False):
            for index in range(0, len(text), 4):
                await emit(MessageDeltaData(message_id=message_id, message_kind=kind, delta=text[index:index + 4]))
                if interleave and index == 8:
                    await emit(TaskProgressData(message="文件信息已准备，继续输出阶段说明"))
                await asyncio.sleep(self.delay)
            await emit(MessageCompletedData(message_id=message_id, message_kind=kind, text=text))

        try:
            source = await self.get_input(conversation_id, turn_id, owner=owner)
            text = (source.text or "") if source is not None else ""
            marker = re.match(r"\s*\[demo:([a-z]+)\]", text)
            scenario = marker.group(1) if marker else self.scenario
            if scenario not in SCENARIOS:
                raise ValueError("未知模拟场景")
            logger.info("联调轮次激活：scenario=%s turn_id=%s", scenario, turn_id)
            await emit(TaskProgressData(message="正在整理输入"))
            await asyncio.sleep(self.delay)
            names = [file.file_name for file in source.files] if source else []
            # 门禁是内部步骤：进度只描述动作，检查结论通过消息进入快照与历史。
            await emit(TaskProgressData(message="正在校验问题与上传文件" if names else "正在校验问题"))
            await asyncio.sleep(self.delay)
            if scenario == "rejected":
                await message("final-1", "final", "该问题与合同业务无关，请重新提出合同相关问题。")
                await emit(TurnStatusData(status="rejected"))
                return
            if scenario == "clarify":
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
            await message("notice-1", "intermediate", "输入校验已通过，接下来将检查相关条款。", interleave=True)
            if scenario == "failure":
                await emit(MessageDeltaData(message_id="notice-2", message_kind="intermediate", delta="正在生成的半段提示……"))
                await asyncio.sleep(self.delay)
                await emit(ErrorData(code="demo_analysis_failed", message="分析服务不可用", retryable=False))
                await emit(TurnStatusData(status="failed"))
                return
            if scenario == "recoverable":
                await emit(ErrorData(code="demo_lookup_retry", message="首次查询失败，正在尝试其他方式", retryable=True))
                await asyncio.sleep(self.delay * 3)
                await emit(TaskProgressData(message="查询已恢复，继续分析"))
            if scenario == "heartbeat":
                await asyncio.sleep(self.heartbeat_seconds * 2.5)
            if scenario == "slow":
                await message("notice-2", "intermediate", "这一步需要一些时间。你可以随时停止，也可以补充要求或调整处理方向。")
                for step in range(10):
                    await emit(TaskProgressData(message=f"正在执行长任务，第 {step + 1}/10 步"))
                    await asyncio.sleep(self.slow_seconds / 10)
            if scenario == "replay":
                # 超过缓存长度，便于前端使用旧游标验证 409 与快照恢复。
                for step in range(self._buffer_size + 20):
                    await emit(TaskProgressData(message=f"回放压力事件 {step + 1}"))
                    await asyncio.sleep(min(self.delay, 0.01))
            await message(
                "plan-1", "intermediate",
                "我将按四步处理：检索相关合同 → 读取关键条款 → 计算付款计划 → 对比差异。"
                "先核对付款与验收约定，再整理需要补充的信息。",
            )
            for step, (tool, title, arguments, progress, result, explanation) in enumerate(DEMO_STEPS, 1):
                call_id = f"demo-call-{step:03d}"
                # 函数名与调用编号仅供日志定位，用户只看到动作和业务条件。
                logger.info("展示工具开始：turn_id=%s call_id=%s tool=%s", turn_id, call_id, tool)
                await self.record_tool_call(conversation_id, turn_id, owner=owner, call_id=call_id,
                                            name=tool, title=title, input_summary=arguments)
                await emit(TaskProgressData(message=f"正在{title} -- {arguments.replace('；', '｜')}"))
                await asyncio.sleep(self.tool_seconds / 2)
                await emit(TaskProgressData(message=f"正在{title} -- {progress}"))
                await asyncio.sleep(self.tool_seconds / 2)
                await emit(TaskProgressData(message=f"{title}已完成 -- {result}"))
                logger.info("展示工具完成：turn_id=%s call_id=%s tool=%s", turn_id, call_id, tool)
                await self.record_tool_result(conversation_id, turn_id, owner=owner, call_id=call_id,
                                              status='succeeded', output_summary=result)
                # 每次调用之后独立流式输出中间结果，维持“计划—调用—结果”的显示顺序。
                # 这是可公开的处理摘要，不模拟或保存模型私有推理链。
                await message(f"step-{step}-result", "intermediate", explanation)
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
                state = await self.snapshot(conversation_id, turn_id, owner=owner)
                if state.status == "processing":
                    logger.exception("模拟执行失败：turn_id=%s", turn_id)
                    await emit(ErrorData(code="demo_driver_error", message="联调脚本执行失败，请检查场景标记和服务日志", retryable=False))
                    await emit(TurnStatusData(status="failed"))
            except (CommunicationNotFoundError, ValueError):
                pass
