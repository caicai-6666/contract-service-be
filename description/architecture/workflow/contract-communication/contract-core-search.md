# 动态 Core 筛选工具

`contract_retrieval/tool/contract_core_search.py` 根据启动期 Core 目录编译 `search_contracts_by_core` 的 Pydantic 参数模型、工具 JSON Schema 和 ES 查询。只在合同查询子 Agent 内注册，需要 ES；主模型仍调用统一 `search_contracts`。不包含合同类型，合同类型由独立的 [类型筛选工具](contract-category-search.md) 处理。

---

## 类型与配置契约

不硬编码当前字段名称或属性路径。字段语义、枚举标签、转换指导、数值边界和倍数来自 definition；新增同类字段无需修改查询实现。工具与对齐节点共享注入的启动快照；查询智能体通过工具 Schema 读取字段定义，不再在 system 中重复注入目录；独立调用从配置目录加载。

| 配置类型 | 操作 | ES 语义 |
| --- | --- | --- |
| 不分词字符串、枚举 | eq | term |
| 分词字符串 | match | match/operator=and，非原文精确相等 |
| integer、number | eq、gt、gte、lt、lte | 精确值及范围；number 允许浮点数 |
| boolean | eq | true/false 严格区分，缺失不等于false |
| strict_date 字符串 | eq、gt、gte、lt、lte | 校验真实 YYYY-MM-DD，再按 date 筛选 |

每个动态属性、操作和值均有 Schema description；枚举、数值范围、multipleOf、基本类型与日期有效性在本地校验。模型不能提交任意字段路径、脚本或 ES DSL。单项单属性压平为 `core.<code>`，单项多属性为 object，多项为 nested，与现有 mapping 规则一致。改变索引类型仍需按现有流程迁移索引，本工具不会迁移旧数据。

---

## 多条件输入

按 Core code 直接选填条件，所有填写条件按 AND 组合。省略或 null 不参与筛选；拒绝空对象、空列表、全 null 和只传 result_id，以免遗漏条件后意外查询全库。

```json
{
  "currency": {"eq": "CNY"},
  "contract_total_amount": {"gte": 100000.5, "lt": 200000},
  "related_parties": [
    {"role": {"eq": "buyer"}, "name": {"match": "现象公司"}}
  ],
  "result_id": null
}
```

单项单属性字段直接填写比较对象，单项多属性填写属性对象，多项字段填写对象列表（1–30项）。每个对象内所有属性条件必须匹配同一个对象；列表中每项都要求存在匹配对象，但不同项可以匹配同一个对象。false 与0均为有效值，不会被当作省略。

金额仅筛选金额值；需求有币种时应同时填写币种，不自动换汇。上下限可同时填写，全部按AND生效；矛盾条件正常产生零结果，不自动放宽。文本只支持match，不保证原文精确相等。

当前不提供 OR、否定、in 或存在性操作。引用上次 result_id 多次筛选可逐步增加 AND 条件，但需要匹配同一个相关方或标的时必须在同一次调用的同一个对象中填写，跨调用只保证合同相同。独立多次查询后可使用 `union_contract_results` 合并两个非空结果集实现 OR；Core 工具自身仍只做 AND 筛选。

---

## 执行与结果

可选 `result_id` 引用当前子 Agent 的候选；省略使用宿主初始范围。空范围直接返回零；失效引用报错，不能回退全库。查询期间与发布前重新检查授权及驻留引用。

执行与类型工具共用 `tool/filter_execution.py`，统一处理分页、权限、范围与失败边界。ES 使用 filter 上下文，不使用 BM25 排名。采用 PIT 与 search_after，每页500条，完整读取后再按 SQLite ready 状态过滤。最多1000次分页请求，超出返回范围过大错误，不提交截断列表。超时、分片失败、重复/非法ID、越界结果或游标不推进均失败；退出时关闭最新 PIT，关闭失败由1分钟租约回收。方案依据 [Elasticsearch 分页说明](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/paginate-search-results)。

成功调用 CandidatePool.filter：父集合有排名时保留综合分数、原分与顺序；首次纯筛选统一记1，标记无相关性排名。后续相关性查询按既有规则处理。只返回结果集ID、数量和成功说明；零结果不生成ID，不暴露合同名单。SSE 使用默认 thinking，内部协议失败与纠错遵循现有子 Agent 清理机制。

此筛选只对入库字段负责；漏提取、未标准化的历史值可能导致漏召回，不能证明原文不存在某项事实。

---

## 验证

`tests/test_contract_core_search.py` 覆盖当前目录全部属性、额外整数/单项多属性配置、金额浮点数、日期有效性、枚举与范围、Schema描述、对象内条件、多对象AND、空条件拒绝及旧格式拒绝、范围白名单、完整分页、父排名继承、空结果、失效/越界和失败不发布，以及子 Agent 调用后结束提交。使用模拟 ES 与生成响应，尚未进行真实模型查询策略或真实 ES 联调。
