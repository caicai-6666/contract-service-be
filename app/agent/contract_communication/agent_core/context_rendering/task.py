"""单任务可读展示；接收程序明确分区的数据，不从消息正文推断终态。"""
from datetime import datetime, timezone, timedelta
import json
import re
from typing import Literal

import yaml
from pydantic import Field, model_validator
from app.schema.communication_workspace import WorkspaceObject, WorkspaceText

from app.schema.communication import ContractReference

TASK_RENDER_VERSION = 'agent-core-task-render-v5'


class TaskAttachment(WorkspaceObject):
    file_id: WorkspaceText = Field(description='程序分配的稳定文件内部索引。')
    file_name: WorkspaceText = Field(description='用户上传文件的原始名称。')
    display_name: WorkspaceText = Field(description='门禁生成的可读展示名称。')
    summary: WorkspaceText = Field(description='已接受的文件摘要，不表示已阅读完整原文。')
    page_count: int = Field(gt=0, description='程序确认的 PDF 实际页数，不能从摘要推测。')


class TaskUserInput(WorkspaceObject):
    content: WorkspaceText | None = Field(default=None, description='用户原始问题；仅有附件时可为空。')
    files: list[TaskAttachment] = Field(default_factory=list, description='按用户上传顺序排列的附件及摘要。')

    contracts: list[ContractReference] = Field(default_factory=list, description='请求接收时读取的正式合同快照，按引用顺序排列。')

    @model_validator(mode='after')
    def validate_input(self):
        if self.content is None and not self.files and not self.contracts:
            raise ValueError('用户输入必须包含问题、附件或引用合同')
        contract_ids = [item.document_id for item in self.contracts]
        if len(contract_ids) != len(set(contract_ids)):
            raise ValueError('引用合同标识不能重复')
        ids = [file.file_id for file in self.files]
        if len(ids) != len(set(ids)):
            raise ValueError('附件内部索引不能重复')
        return self


class TaskTraceEntry(WorkspaceObject):
    kind: Literal['tool_call', 'tool_result', 'intermediate', 'system_guidence', 'assistant'] = Field(
        description='程序确认的轨迹类型；不根据内容中的标签猜测来源。')
    content: WorkspaceText = Field(description='工具调用原始参数、工具反馈文本或该条交互正文，不含私有推理和审计。')
    name: WorkspaceText | None = Field(default=None, description='工具名称；tool_call 必填，tool_result 可选，其他类型省略。')
    call_id: WorkspaceText | None = Field(default=None, description='工具调用及反馈的对应标识；这两种类型必填。')

    @model_validator(mode='after')
    def validate_tool_identity(self):
        if self.kind in {'tool_call', 'tool_result'}:
            if self.call_id is None or (self.kind == 'tool_call' and self.name is None):
                raise ValueError('工具轨迹缺少调用标识或工具名称')
        elif self.name is not None or self.call_id is not None:
            raise ValueError('非工具轨迹不能携带工具身份')
        return self


class TaskFinalOutput(WorkspaceObject):
    kind: Literal['completed', 'interrupted', 'superseded', 'failed'] = Field(
        description='程序确认的最终输出、用户终止、用户调整方向或执行失败。')
    content: WorkspaceText | None = Field(default=None, description='正常完成时必须提供已提交的最终正文；其他结束方式可省略，仅在已有真实结束说明时提供，不生成业务结论。')

    @model_validator(mode='after')
    def validate_completed_content(self):
        if self.kind == 'completed' and self.content is None:
            raise ValueError('正常完成必须包含实际最终输出正文')
        return self


class TaskRenderInput(WorkspaceObject):
    task_number: int = Field(gt=0, description='由调用方提供的展示序号，不替代稳定 task_id。')
    task_id: WorkspaceText = Field(description='程序持有的稳定任务标识。')
    created_at: int | None = Field(default=None, ge=0, description='任务创建时间，UTC Unix毫秒；旧的独立渲染输入未知时可省略，正式上下文从任务记录传入。')
    user_input: TaskUserInput = Field(description='用户问题与附件目录。')
    execution_trace: list[TaskTraceEntry] = Field(default_factory=list, description='按实际顺序排列的模型可见轨迹；已成功的最终输出正文单独置于 final_output，不重复放入此列表。')
    final_output: TaskFinalOutput | None = Field(default=None, description='当前任务没有最终输出；存在时表明任务已封闭，不由渲染函数改变状态。')


def _block(text: str, *, language: str = 'text') -> str:
    fence = '`' * max(3, 1 + max((len(run) for run in re.findall(r'`+', text)), default=0))
    return f'{fence}{language}\n{text}\n{fence}'


def _metadata(data: dict) -> str:
    return _block(yaml.safe_dump(data, allow_unicode=True, sort_keys=False).rstrip('\n'), language='yaml')


def _input_lines(value: TaskRenderInput) -> list[str]:
    # 标识使用 JSON 字符串展示，避免多行标识伪造任务标题。
    identity = json.dumps(value.task_id, ensure_ascii=False)
    lines = ['━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',
             f'任务 {value.task_number} · {identity}',
             '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━']
    if value.created_at is not None:
        # 使用固定业务时区，避免服务宿主机时区改变或恢复会话时改用当前时间。
        created = datetime.fromtimestamp(value.created_at / 1000, tz=timezone.utc).astimezone(timezone(timedelta(hours=8)))
        lines += [f'任务创建时间：{created.isoformat(timespec="milliseconds")}（北京时间）']
    lines += ['', '## 用户输入']
    if value.user_input.content is not None:
        lines += ['', '### 用户问题', '', _block(value.user_input.content)]
    for index, file in enumerate(value.user_input.files, 1):
        lines += ['', f'### 附件 {index}', '', _metadata({
            '内部索引': file.file_id, '文件名': file.file_name,
            '展示名称': file.display_name, '页数': file.page_count}),
            '', '摘要：', _block(file.summary)]
    if value.user_input.contracts:
        lines += ['', render_contract_references(value.user_input.contracts)]
    return lines


def render_contract_references(contracts, *, heading='### 引用合同') -> str:
    """只展示引用快照的三项信息；正文使用安全代码围栏，不注入 PDF 页面。"""
    lines = []
    for index, raw in enumerate(contracts, 1):
        item = ContractReference.model_validate(raw)
        lines += [f'{heading} {index}', '', _metadata({
            '合同 ID': item.document_id, '文件名': item.file_name}),
            '', '摘要：', _block(item.summary or '该合同尚未保存摘要。'), '']
    return '\n'.join(lines).rstrip()


def render_task_input(task: TaskRenderInput | dict) -> str:
    """仅展示当前任务身份与用户输入；工具交互由原生消息携带。"""
    value = TaskRenderInput.model_validate(task)
    if value.final_output is not None:
        raise ValueError('已封闭任务应使用完整任务渲染')
    return '\n'.join(_input_lines(value))


def render_task_section(task: TaskRenderInput | dict, *, reasoning_by_call=None, final_call_id=None) -> str:
    """保留顺序和正文；当前任务不渲染最终输出标题、占位或结束分隔线。

    轨迹来源和终态由调用方确认。此函数不负责协议恢复清理、持久化、
    从 FIFO 消息或用户展示历史中自动拆分任务，也不参与当前 JSON 计数。
    """
    if isinstance(task, TaskRenderInput):
        task = task.model_dump()
    value = TaskRenderInput.model_validate(task)
    lines = _input_lines(value)
    lines += ['', '────────────────────────────────────────', '## 执行轨迹']
    labels = {'tool_call': '工具调用', 'tool_result': '工具反馈', 'intermediate': '中途输出',
              'system_guidence': '系统提示', 'assistant': '助手说明'}
    reasoning_by_call = reasoning_by_call or {}
    def thought_lines(call_id):
        item = reasoning_by_call.get(call_id)
        if item is None:
            return []
        text = '\n\n'.join(item.fields.values())
        return ['', f'### 保留思考 · 位置 {item.position}', '',
                '以下为当时的分析过程，不等于已核实事实。', _block(text)]
    for index, entry in enumerate(value.execution_trace, 1):
        if entry.kind == 'tool_call':
            lines += thought_lines(entry.call_id)
        lines += ['', f'### 步骤 {index} · {labels[entry.kind]}', '']
        if entry.call_id is not None:
            metadata = {'调用标识': entry.call_id}
            if entry.name is not None:
                metadata['工具名称'] = entry.name
            lines += [_metadata(metadata), '']
        lines.append(_block(entry.content))
    if value.final_output is not None:
        lines += thought_lines(final_call_id)
        endings = {'completed': '最终反馈', 'interrupted': '用户终止',
                   'superseded': '用户调整方向', 'failed': '执行失败'}
        lines += ['', '────────────────────────────────────────', '## 最终输出', '',
                  f'结束方式：{endings[value.final_output.kind]}']
        # 终止、替代或失败通常只有程序状态；不要求生成说明，也不补写业务结论。
        if value.final_output.content is not None:
            lines += ['', _block(value.final_output.content)]
        lines += ['', f'━━━━━━━━━━━━ 任务 {value.task_number} 结束 ━━━━━━━━━━━━']
    return '\n'.join(lines)
