"""十组虚构多轮样例；与实际输入共用渲染器，不读取真实会话。"""

import json
import random
from dataclasses import dataclass

from app.schema.communication import ConversationHistoryRecord
from ..schema import ContextRelevanceBasis
from ..state import FileSummary
from .context_rendering import render_context_relevance_input


@dataclass(frozen=True)
class ContextRelevanceExample:
    example_id: str
    input_text: str
    evidence: tuple[str, ...]
    reasoning: str
    basis: ContextRelevanceBasis
    result: bool

    def output_json(self) -> str:
        return json.dumps(dict(evidence=self.evidence, reasoning=self.reasoning, basis=self.basis, result=self.result),
                          ensure_ascii=False, separators=(',', ':'))


@dataclass(frozen=True)
class _Turn:
    text: str
    answer: str | None = None
    files: tuple[tuple[str, str], ...] = ()
    status: str = 'completed'


def _example(number, *, turns, text, files=(), evidence, reasoning, result, basis=None):
    records = []
    for i, turn in enumerate(turns, 1):
        records.append(ConversationHistoryRecord(
            record_id=f'example-record-{i}', turn_id=f'example-turn-{i}', sequence=i,
            kind='task', status=turn.status, created_at=0,
            payload={'input': {'text': turn.text, 'files': [
                dict(display_name=name, summary=summary, admission='accepted') for name, summary in turn.files
            ]}, 'trace': [] if turn.answer is None else [dict(
                type='message', message_id='example-final', message_kind='final',
                status='completed', text=turn.answer,
            )]},
        ))
    current_files = tuple(FileSummary(file_index=i, original_file_name='example-hidden.pdf',
                                     display_name=name, summary=summary)
                          for i, (name, summary) in enumerate(files))
    return ContextRelevanceExample(
        example_id=f'context-example-{number:02}',
        input_text=render_context_relevance_input(history=records, text=text, file_summaries=current_files),
        evidence=tuple(evidence), reasoning=reasoning, result=result,
        basis=basis or ('history_continuation' if result else 'none'),
    )


# 正负各五组只约束样例池；请求抽样不按标签配额，不强制正负交替。
CONTEXT_RELEVANCE_EXAMPLES = (
    _example(1, turns=(
        _Turn('这份尾款安排帮我看一下，我们设备已经送过去了。',
              '这里把尾款与验收合格挂钩。你方是收取尾款的设备供应商，还是付款的采购方？',
              (('自动分拣线采购合同', '约定分拣线供货、安装调试及验收，验收合格后支付合同价款的20%。'),)),
        _Turn('是我们供的货，客户那边还没安排试机。',
              '需要区分已经交货和已经验收。合同里是否约定客户应在收到调试通知后多久配合试机？'),
        _Turn('还有一个小问题，历史列表能按签订日期倒着排吗？',
              '可以按签订日期从新到旧查看；当前这份合同的尾款问题仍需核对验收配合约定。'),
    ), text='还有别的补充吗？',
        evidence=('历史第1轮助手最终回答：‘这里把尾款与验收合格挂钩。’',
                  '历史第2轮用户：‘客户那边还没安排试机。’',
                  '本轮文字：‘还有别的补充吗？’'),
        reasoning='可见历史持续讨论客户未试机与验收尾款，最后回答也保留了该事项。本轮请求进一步补充，虽没有复述业务关键词仍构成承接；不在此认定客户违约或付款义务到期。', result=True),
    _example(2, turns=(
        _Turn('冷却塔这个项目，交付日期有没有超？',
              '合同约定9月12日前到货；现有材料没有能确认实际到货日期的记录。',
              (('南区冷却塔供货合同', '供应南区冷却塔及配件，约定9月12日前到货，并规定到货检查和安装安排。'),)),
        _Turn('安装的人是16号来的，能不能直接按16号算？',
              '安装人员到场不一定等于设备到货。请补充该项目的送货或签收记录。'),
    ), text=None, files=(
        ('南区冷却塔设备签收单', '记录南区冷却塔及配件于9月11日签收，列有设备数量、外包装检查和收货人签字。'),
        ('机修车间安全培训签到表', '记录机修车间人员参加用电安全培训的签到情况。'),
    ), evidence=('历史第2轮助手最终回答：‘请补充该项目的送货或签收记录。’',
                 '本轮文件第1份文件名称：‘南区冷却塔设备签收单’',
                 '本轮文件第1份内容摘要：‘记录南区冷却塔及配件于9月11日签收’'),
        reasoning='本轮第一份材料对应历史要求补充的到货证据，只有文件也构成延续；第二份培训材料不改变这一关联，不据摘要完成逾期判断。', result=True),
    _example(3, turns=(
        _Turn('这两版续租条件差在哪？', '两版主要区别是押金处理和退租恢复要求。',
              (('东库房续租方案A', '续租两年，保留现有押金，退租时拆除承租方增设的隔断。'),
               ('东库房续租方案B', '续租一年，增加一个月押金，退租恢复范围由交接清单确认。'))),
        _Turn('做个表吧，把租金、押金、装修恢复都列上。', status='superseded'),
    ), text='表先不用了。第二个里面，交接清单没写的东西也要我们拆吗？',
        evidence=('历史第1轮用户文件第2份内容摘要：‘退租恢复范围由交接清单确认。’',
                  '历史第2轮用户：‘做个表吧，把租金、押金、装修恢复都列上。’',
                  '本轮文字：‘第二个里面，交接清单没写的东西也要我们拆吗？’'),
        reasoning='本轮取消表格表达方式，转而追问第二份方案的恢复范围，是原比较任务的方向调整；不因上一轮没有最终回答而否定关联，也不推断清单之外的拆除义务。', result=True),
    _example(4, turns=(
        _Turn('帮我算一下这批控制柜目前还差多少款。',
              '需要核对合同总价、已付款及退货或折让情况。',
              (('控制柜采购结算单', '列出控制柜合同总价48万元，已付24万元，另列一笔待确认的退货折让。'),)),
        _Turn('折让是两万，不是两千，你先按两万算。', status='cancelled'),
        _Turn('我看了下回单，已付应该是26万。重新算下。', status='failed'),
    ), text='就按26万和2万那两个数，给我个余额就行。',
        evidence=('历史第1轮用户文件第1份内容摘要：‘合同总价48万元’',
                  '历史第2轮用户：‘折让是两万，不是两千’',
                  '历史第3轮用户：‘已付应该是26万。重新算下。’',
                  '本轮文字：‘就按26万和2万那两个数，给我个余额就行。’'),
        reasoning='本轮明确引用此前修正的付款与折让数值并请求余额，关联成立；取消和失败没有生成可用结果，本轮是新的明确请求，不在此执行计算或确认回单事实。', result=True),
    _example(5, turns=(
        _Turn('这份协议里限制我们招他们员工的那段，能不能删掉？',
              '需要结合你方合作目标判断，你是希望完全删除，还是缩短限制期限？',
              (('研发联合测试合作协议', '约定联合测试安排、保密义务及合作期间和结束后一年内的人员招揽限制。'),)),
        _Turn('业务说不想删得太僵，半年可以接受。',
              '可以将结束后的限制期限改为半年，并进一步明确适用人员范围。'),
        _Turn('那你帮我把这个意思写得客气一点。',
              '建议将合作结束后的限制期限调整为六个月，同时将范围限定为直接参与本项目的人员。'),
    ), text='这件事先放着，不用参考之前的协议，给我做一份周末健身计划。',
        evidence=('历史第3轮用户：‘把这个意思写得客气一点’',
                  '历史第3轮助手最终回答：‘建议将合作结束后的限制期限调整为六个月’',
                  '本轮文字：‘不用参考之前的协议，给我做一份周末健身计划。’'),
        reasoning='本轮明确搁置协议事项并排除旧文件，实际要求是独立的健身计划；提到之前的协议不是操作它的意图，不能据此认定上下文相关。', result=False),
    _example(6, turns=(
        _Turn('东港仓库的押金什么时候能拿回来？',
              '这份租赁合同将押金返还与腾退交接、费用结清挂钩。',
              (('东港仓库租赁合同', '约定退租后办理场地交接并结清水电费用，完成后返还租赁押金。'),)),
        _Turn('电费已经结了，但钥匙还在我们这。',
              '费用结清与场地交接是不同条件，需要再核对钥匙及场地交接安排。'),
    ), text='投标时交的那笔保证金，没中标多久退？',
        evidence=('历史第1轮用户文件第1份文件名称：‘东港仓库租赁合同’',
                  '历史第1轮助手最终回答：‘押金返还与腾退交接、费用结清挂钩’',
                  '本轮文字：‘投标时交的那笔保证金，没中标多久退？’'),
        reasoning='本轮没有要求操作先前文件，不满足文件意图例外。历史是仓库租赁押金，本轮是未中标后的投标保证金；可见资料未建立具体联系，不能仅因都涉及退钱认定延续。', result=False),
    _example(7, turns=(
        _Turn('看一下研发中心装修合同，哪些地方还要我们确认？',
              '可以先确认施工范围和验收标准；目前附件没有完整的材料清单。',
              (('研发中心装修施工合同', '约定办公室装修施工、材料确认与竣工验收，材料清单作为另附文件。'),)),
        _Turn('材料清单我明天找施工方要。',
              '收到清单后可与合同约定的施工范围一并核对。'),
    ), text='这个含税价是不是算错了？', files=(
        ('食堂大米采购报价单', '供应商报送食堂大米的规格、每袋数量、未税单价和税额，列出本批采购含税总价。'),
    ), evidence=('历史第1轮用户文件第1份文件名称：‘研发中心装修施工合同’',
                 '本轮文字：‘这个含税价是不是算错了？’',
                 '本轮文件第1份内容摘要：‘供应商报送食堂大米的规格、每袋数量、未税单价和税额’'),
        reasoning='本轮操作的是本次新上传的大米报价，不是先前文件，因此不满足历史文件意图例外。大米核价与装修及材料清单没有可见联系，不能仅凭通用业务词汇关联。', result=False),
    _example(8, turns=(
        _Turn('这个月差旅报销有多少笔还缺票？',
              '清单中有三笔尚未关联发票，需要补充相应票据。',
              (('八月差旅报销核对表', '列出员工差旅报销日期、交通住宿金额与发票关联状态。'),)),
        _Turn('先把名单给我，我找他们补。', '待补票名单为王工、陈工和李工，请以原报销清单核对。'),
        _Turn('陈工的已经补好了。', '可以在差旅核对表中更新陈工对应记录，其他两笔仍需确认。'),
    ), text='上周那份保密协议，离职以后也得保密吗？',
        evidence=('历史第1轮用户：‘这个月差旅报销有多少笔还缺票？’',
                  '历史第3轮用户：‘陈工的已经补好了。’',
                  '本轮文字：‘上周那份保密协议，离职以后也得保密吗？’'),
        reasoning='用户明确要求依据上周的保密协议判断离职后的保密义务，但近期差旅历史未支持这一事项，只有先前文件操作意图。尚未定位或读取该文件，不推断员工对应关系或具体保密义务。', result=True, basis='prior_file_reference'),
    _example(9, turns=(
        _Turn('红外检测仪的维护合同里，响应时间是怎么算的？',
              '这份合同写的是收到故障通知后两小时内响应，响应与到场维修需要分别看。',
              (('红外检测仪年度维护协议', '约定故障通知后的响应时限、远程排查及现场维修服务。'),)),
        _Turn('昨天通知了，今天才有人回我，这个先帮我留着。',
              '你提出的是故障通知与实际回应时间的差异；具体是否超过约定仍需核对通知时间及通知方式。'),
    ), text=None, files=(
        ('厂区团建活动安排', '说明徒步集合时间、交通分组、午餐地点及恶劣天气下的活动取消安排。'),
        ('篮球友谊赛参赛名单', '列出各部门参赛人员、队伍分组及赛程。'),
    ), evidence=('历史第1轮用户文件第1份文件名称：‘红外检测仪年度维护协议’',
                 '本轮文件第1份内容摘要：‘说明徒步集合时间、交通分组、午餐地点及恶劣天气下的活动取消安排。’',
                 '本轮文件第2份文件名称：‘篮球友谊赛参赛名单’'),
        reasoning='本轮只有新上传的活动与赛事材料，没有操作先前文件的文字意图；新材料也未显示与仪器故障响应事项有具体联系，不能因同在厂区补造共同目标。', result=False),
    _example(10, turns=(
        _Turn('青禾物流这张运输合同，保价条款在哪？',
              '现有摘要提到了货损赔偿，但尚不能据此确认是否另有保价约定。',
              (('青禾物流运输服务合同', '约定公路运输、交接凭证及货损赔偿，摘要未说明保价选项。'),)),
        _Turn('你再仔细找一下。', status='cancelled'),
    ), text='周五我和小周去苏州见客户，住一晚的话住宿费能报多少？',
        evidence=('历史第1轮用户：‘青禾物流这张运输合同，保价条款在哪？’',
                  '本轮文字：‘周五我和小周去苏州见客户，住一晚的话住宿费能报多少？’'),
        reasoning='本轮没有要求根据先前文件处理，不满足文件意图例外；当前是员工出差住宿报销，与历史运输合同保价没有可见事项关联，不能因都涉及出行或费用认定延续。', result=False),
)


def sample_context_relevance_examples(*, rng: random.Random | None = None) -> tuple[ContextRelevanceExample, ...]:
    """每次从十组中无放回抽四组，顺序也随机；可注入局部 RNG 复现实验。"""
    return tuple((rng if rng is not None else random.SystemRandom()).sample(CONTEXT_RELEVANCE_EXAMPLES, 4))


def render_context_relevance_examples(examples) -> str:
    selected = tuple(examples)
    if (len(selected) != 4 or len({row.example_id for row in selected}) != 4
        or any(row not in CONTEXT_RELEVANCE_EXAMPLES for row in selected)):
        raise ValueError('必须提供样例池中互不重复的4组样例')
    blocks = ['## 判断示例\n\n以下均为独立虚构案例，不是当前用户历史。每组输入区域与实际任务一致。']
    for number, example in enumerate(selected, 1):
        blocks.append(f'### 示例{number}输入\n\n{example.input_text}\n\n'
                      f'### 示例{number}输出\n\n{example.output_json()}')
    return '\n\n---\n\n'.join(blocks)
