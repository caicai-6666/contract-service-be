# 按合同名称检索

`search_contracts_by_name` 适用于已知或能够依据现有线索拟写合同名称的场景，只以 SQLite BM25 匹配 `contracts.file_name`，不调用 Embedding，也不检索摘要和备注。现作为[合同候选查询子图](contract-retrieval.md)内部工具。

---

## 工具契约

| 参数 | 必填 | 含义 |
| --- | --- | --- |
| `query` | 是 | 1–1000 字符，“xxx合同”式的简短名称，例如“生产设备采购合同”“仓库租赁合同”。 |
| `result_id` | 否 | 本次子图调用的内部结果集完整引用；省略或 null 使用宿主限定的初始范围，未限定时查询全部 ready 合同，传入则限制合同范围。 |

查询使用名称短语，不写成“帮我找……”式请求、问题或摘要；保留已知主体、项目、标的和编号，不猜测未知信息。这是模型参数描述中的风格约束，程序不强制名称以“合同”结尾。

成功返回有限候选的结果集 ID 和数量，供子 Agent 继续查询或收束，不自动展开第一页。零结果不创建引用，失效范围报错、不退回全库。查询期间原引用驱逐或会话失效时不发布结果。

---

## 索引和排序

`contract_names_fts` 使用 Lindera（Jieba）的 FTS5 外部内容索引，关联 contracts.rowid。首次初始化回填历史名称，增删和名称修改由触发器与元数据在同一事务同步；重复初始化不重建。维护数据库必须使用加载 Lindera 的连接。

查询使用相同分词器切词，逐词引用转义后按 OR 匹配。ready 和输入范围在排名截断前过滤。SQLite BM25 原始分数越小越好，返回时取负以统一降序展示，并以 document_id 确定并列顺序。单路检索不使用 RRF，也不融合源结果集分数。常见词“合同”可能召回弱相关候选，有限结果不等于准确命中清单或全量符合条件的合同。

内部结果池只驻留 ID、分数和查询方式，查询后只回显引用与数量，不提供 view 工具。最终名单由程序读取，详见[合同候选查询子图](contract-retrieval.md)。

---

## 配置与验证

`COMMUNICATION_CONTRACT_NAME_SEARCH_TOP_K` 默认 10，范围 1–50；驻留容量及页大小复用 `COMMUNICATION_CONTRACT_SEARCH_*`。

实现为 `agent_core/subgraph/contract_retrieval/tool/contract_name_search.py` 和 `infrastructure/contract_name_search.py`。真实临时 SQLite/Lindera 测试覆盖排名、范围、状态、历史回填、增删改同步、空结果、失效引用、失败反馈、参数描述和分页标签。主循环装配测试使用模拟生成验证搜索→查看→最终回复，尚未测试真实模型拟写名称的稳定性。
