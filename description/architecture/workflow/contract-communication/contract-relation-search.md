# 合同关系检索

`search_contract_relations` 根据用户记录的关系描述查找边，输出独立关系结果集；`view_contract_relation_search_results` 分页展示两端合同及关系。已接入 Agent Core。搜索状态为 `local-search`，查看为 `thinking`，页面为 foldable。

---

## 查询契约

| 参数 | 必填 | 含义 |
| --- | --- | --- |
| `query` | 是 | 1–10000 字符的拟写关系备注，用陈述句描述关系类型、角色、原因及已知条件。 |
| `contract_id` | 否 | 完整 64 位小写 SHA-256；省略或 null 全库查询，指定时先限定一跳邻边。 |

例如“补充协议调整原合同的付款安排”“新合同替代旧合同”。只知道属于同一项目时，不额外推断采购、施工等角色；保留条件、否定和不确定性。边虽无向，描述中的合同角色不能交换。

指定合同不存在或非 ready、图节点缺失均报错，不降级全库。两端都必须在 SQLite 中 ready，先过滤后排名。成功非空返回 result_id 和边数，使用独立查看工具读取第一页；空结果不生成 ID。不接收普通合同结果集作为范围，不把边归并成合同。

---

## 向量与 BM25

存储侧保持 `contract-relation-description-v1`。查询侧为 `contract-relation-query-v1`，ChatML user 直接放 query，system 为：

```text
Represent this description of a desired relationship between contracts
 to retrieve matching user-authored relationship descriptions.
Focus on the relationship type, the stated roles of the contracts,
the reason for their association, and explicit conditions.
Preserve negation and uncertainty. Use only the supplied information;
do not infer missing roles or facts.
```

Neo4j 保存唯一关系原文及向量；程序先获取指定范围的边快照，在查询内存 SQLite 中使用 Lindera（Jieba）做 BM25，sqlite-vec 做精确余弦排序。BM25、向量使用同一 query；仅向量输入附带指令。没有新增 Neo4j 全文或向量索引，也没有持久化第二份关系副本。关系新建、删除下次查询自动可见。

两路各取 `min(top_k × 4, 200)` 条，以等权 RRF `sum(1 / (60 + rank))` 融合，rank 从 1 开始；缺席一路贡献零，不调整分母。并列按关系 ID 排序。模型、版本、维度不匹配或缺向量的关系仍可参加 BM25。向量不设相似度下限，可能召回弱相关候选；RRF 不是概率。

首次拉取设 10000 条边安全上限，超过即失败，不截断后伪装为全量检索；建议指定合同缩小范围。当前方案面向中小规模关系库，全库请求会读取候选描述及向量，未来数据增长需考虑数据库原生索引。中文分词受上下文影响，词面近似不保证 BM25 命中，向量用于补充语义召回。

---

## 驻留、渲染与分页

每会话独立池，只保存关系 ID、RRF 分数、起点和游标，不保存渲染文本。沿用签名引用、LRU 和会话驱逐机制，但使用 `relation-search:` 独立命名空间。普通合同查看工具不能使用此引用。会话释放后迟到结果不重新建池。

查看参数为 result_id 和可选 page。省略首次第 1 页、后续下一页；显式页码可重看。非法页和末页提示分开，均不移动游标。分页实时读取 Neo4j 关系及 SQLite 两端合同名称、摘要，删除关系或任一端不可用时显示失效提示。新建替代边不会占用旧 ID。

每条边按以下结构展示：

```text
关联 N / 关系 ID / RRF 综合分数
  合同 A：完整 ID、名称、摘要
  合同 B：完整 ID、名称、摘要
  关系描述
```

指定起点固定为 A，其余情况按存储端点顺序展示；A/B 只是展示位置，不表示法律角色或关系方向。关系描述来自用户，不是合同原文，需根据页面信息选择合同再查看原文。

---

## 配置与验证

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `COMMUNICATION_RELATION_SEARCH_TOP_K` | 10 | 最多返回边数，1–50。 |
| `COMMUNICATION_RELATION_SEARCH_CACHE_MAX_QUERIES` | 10 | 每会话驻留结果集上限。 |
| `COMMUNICATION_RELATION_SEARCH_PAGE_SIZE` | 3 | 每页边数，1–50。 |

工具与渲染器位于 `tool/contract_relation_search.py`，排名在 `service/contract_relation_search.py`；范围读取由 ContractGraphStore / ContractRelationService 提供。测试覆盖实际 Lindera/vec、RRF、缺向量、起点筛选、查询指令、两端渲染、删除、分页、释放及正式循环装配。生成、Embedding 和 Neo4j 响应使用替身，本轮尚未进行真实模型与 Neo4j 联调。
