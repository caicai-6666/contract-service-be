# 检索问题指南定义结构

> **用途：** 本文是 `data/definition/retrieval-view/question` 的用户配置契约，说明通用提问指南与领域提问指南的 YAML 结构、字段含义和扩展方式。

本目录只定义模型如何规划和生成适合合同召回的问题，不保存模型输出、问题答案、向量或 Elasticsearch 文档。工作流位置与问题生成状态见[检索问题生成子图](../workflow/contract-extraction/retrieval-view-generation.md)。

---

## 目录结构

```text
data/definition/retrieval-view/
  question/
    common.yaml
    category/
      <category-code>.yaml
```

| 文件 | 数量 | 职责 |
| --- | ---: | --- |
| `question/common.yaml` | 固定一个 | 定义所有合同共享的选题原则、问题表达规则和通用关注点。 |
| `question/category/<code>.yaml` | 每个领域一个 | 定义某个领域补充的专业提问关注点。 |

`category` 是指南的组织命名空间，不是运行时分类路由。问题规划会看到通用指南与全部领域指南；正式问题生成只渲染当前规划选中的关注点。

> **已删除边界：** 项目不再提供 `answer/` 目录、身份骨架、公共回答规则、领域回答规则或提问—回答配对约束。旧 `answer/` 目录会被启动加载器视为未定义条目并明确拒绝。

---

## 全局格式约束

- YAML 顶层必须是对象，不能是列表或纯文本。
- 所有字符串会去除首尾空白，去除后不能为空。
- `code` 与 `category_code` 使用 snake_case，只允许小写字母、数字和下划线，并且必须以字母开头。
- Schema 中声明为列表的字段必须使用 YAML 列表；要求至少一项的列表不能留空。
- 同一规则列表中的文本、同一对象中的关注点 code 和同一目录中的领域 code 不能重复。
- 不允许增加 Schema 未声明的字段；`schema_version`、备注字段和运行时结果都会导致启动加载失败。
- YAML 注释面向维护者，不参与对象解析。模型看到的是包含中文含义标签的确定性 Bullet，而不是原始 YAML。
- 修改文件后必须重启应用；启动时形成的不可变目录快照不会在运行期间自动变化。

领域文件名由 `category_code` 确定：将 code 中的下划线替换为连字符，再追加 `.yaml`。例如 `general_service` 必须保存为 `general-service.yaml`。

---

## 通用提问指南

`question/common.yaml` 只能有一个，完整结构如下：

```yaml
# name：指南名称；purpose：指南总目标。
# selection_rules：从全部候选关注点中选择问题的集合级规则。
# question_rules：每个问题的表达规则。
# attention_points：所有合同均可参考的通用关注点。
name: 合同检索问题通用选题指南
purpose: 从合同法律关系和真实履约过程发现高价值问题。
selection_rules:
  - 先判断关注点是否适用于当前合同，再决定是否生成问题。
question_rules:
  - 每个问题只表达一个主要检索意图。
  - 模拟真实业务人员查找合同时会输入的自然中文，并保留必要交易背景。
attention_points:
  - code: transaction_relation
    name: 交易关系与合同目的
    legal_significance: 说明该事项为什么影响合同权利义务。
    practice_significance: 说明该事项为什么影响真实履约或检索价值。
    applicable_when:
      - 当前文件能够识别签约主体和核心给付关系
    inspect_for:
      - 各方在交易中的实际角色
    material_if_missing:
      - 核心交易内容或各方角色不清楚
    excludes:
      - 仅重复合同名称、编号或签署日期
```

顶层字段：

| 字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `name` | 非空字符串 | 是 | 指南的人类可读名称。 |
| `purpose` | 非空字符串 | 是 | 通用提问指南要实现的整体目标。 |
| `selection_rules` | 非空字符串列表 | 是 | 控制问题集合的适用性、优先级、去重和数量边界。 |
| `question_rules` | 非空字符串列表 | 是 | 控制单个问题的语义粒度和表达方式。 |
| `attention_points` | 关注点对象列表 | 是 | 通用候选关注点，至少包含一个。 |

每个 `attention_points` 元素：

| 字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `code` | snake_case 字符串 | 是 | 通用关注点内唯一的稳定标识；渲染后使用 `common.<code>`。 |
| `name` | 非空字符串 | 是 | 面向模型和维护者的关注点名称。 |
| `legal_significance` | 非空字符串 | 是 | 该关注点对权利义务或合同目的的意义。 |
| `practice_significance` | 非空字符串 | 是 | 该关注点对真实履约、风险或检索的意义。 |
| `applicable_when` | 非空字符串列表 | 是 | 允许考虑该关注点的适用条件。 |
| `inspect_for` | 非空字符串列表 | 是 | 模型应在合同中核查的事实。 |
| `material_if_missing` | 非空字符串列表 | 是 | 合同未明确约定时仍值得形成问题的重大情形。 |
| `excludes` | 非空字符串列表 | 是 | 容易混淆但不属于该关注点的事项。 |

`question_rules` 应把“贴近用户提问”定义为可执行风格：原文能够确认时，问题应带产品、服务、项目或必要的相关方简称等交易锚点；优先采用“谁做什么、什么时候做、满足什么条件、涉及多少”等业务表达；允许用一至两个紧密问句完整询问同一履约机制。不得把问题写成字段标签、审查清单或机械模板。

---

## 领域提问指南

`question/category/<code>.yaml` 每个文件定义一个领域：

```yaml
# category_code：领域稳定标识；category_name：领域中文名称。
# purpose：该领域补充指南的目标；selection_rules：领域内选题规则。
# attention_points：该领域特有或需要深化的关注点。
category_code: sale
category_name: 买卖
purpose: 发现影响货物交付、验收、价款、风险和售后责任的问题。
selection_rules:
  - 以实际货物交付和价款交换为依据，不只看合同标题。
attention_points:
  - code: delivery_ownership_and_risk
    name: 交付、所有权与风险转移
    legal_significance: 交付与风险安排决定货物由谁控制并承担毁损灭失后果。
    practice_significance: 运输、签收和验收通常决定付款及责任起点。
    applicable_when:
      - 合同需要将货物交接到指定地点
    inspect_for:
      - 交货时间、地点、方式和风险转移时点
    material_if_missing:
      - 货物运输或安装期间具有明显毁损灭失风险
    excludes:
      - 没有合同依据的默认风险转移结论
```

| 顶层字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `category_code` | snake_case 字符串 | 是 | 领域稳定标识；必须存在于合同类别权威目录中，并与文件名一致。 |
| `category_name` | 非空字符串 | 是 | 领域标准中文名称。 |
| `purpose` | 非空字符串 | 是 | 该领域为什么需要补充通用提问指南。 |
| `selection_rules` | 非空字符串列表 | 是 | 该领域的适用、去重和边界规则。 |
| `attention_points` | 关注点对象列表 | 是 | 结构与通用关注点一致，code 在当前领域内唯一。 |

领域关注点的稳定身份是 `<category_code>.<attention_point.code>`，例如 `sale.delivery_ownership_and_risk`。该标识只用于问题规划、精确选择指南和审计，不再承担回答指南配对职责。

---

## 用户新增领域指南

1. 确认领域 code 已存在于[合同交易类别定义结构](contract-category-definition.md)描述的权威类别目录。
2. 在 `question/category` 新建 `<category-code>.yaml`，定义领域目的、选择规则和至少一个提问关注点。
3. 检查新关注点是否与通用或其他领域关注点重复；能由同一个问题覆盖时优先复用或合并语义。
4. 运行目录与渲染测试，确认 Schema、文件名、类别引用和稳定渲染全部通过。
5. 重启 FastAPI 应用，使新目录形成新的不可变内存快照和内容指纹。

不要通过复制大量近义领域文件扩大覆盖率。领域指南的价值在于补充真实专业差异；通用法律与履约结构由 `question/common.yaml` 负责。

---

## 验证方式

```bash
PYTHONPATH=. pytest -q \
  test/test_retrieval_view_guide_catalog.py \
  test/test_retrieval_view_guide_prompt.py
```

测试覆盖：

- 两类提问 YAML 能够加载为严格、不可变对象。
- 同一目录内容产生稳定指纹。
- 未知合同类别和旧 `answer/` 目录会明确失败。
- YAML 被渲染为带中文含义标签的 Bullet，而不是原样注入模型。
- 全量指南与按规划选择的指南都保持稳定顺序。

正式对象契约位于 `app.agent.contract_extraction.subgraph.retrieval_view_generation.definition`，目录加载器位于同包的 `catalog` 模块。Python Schema 与启动校验是最终机器约束。
