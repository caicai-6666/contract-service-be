# 合同类型筛选工具

`contract_retrieval/tool/contract_category_search.py` 提供 `search_contracts_by_category`，只在合同查询子 Agent 内注册。由启动期合同类型 definition 动态生成参数枚举，查询已入库类别，不根据合同名称猜测类型。需要 ES 客户端，SSE沿用默认thinking。

---

## 参数与动态编译

| 参数 | 要求 | 含义 |
| --- | --- | --- |
| category | 必填，字符串枚举 | 一个标准类别code；枚举及名称、含义说明来自category_catalog |
| result_id | 可选 | 当前子图候选结果引用；省略或null使用宿主初始范围 |

`compile_category_arguments` 生成 Pydantic 模型和实际JSON Schema，所有参数都有description。未知类型、中文名称替代code、列表、空引用或额外参数在执行前拒绝。新增类别无需改工具代码；重启加载目录后生效。空类别目录不能编译工具。

目录快照由workflow同时传给对齐与查询节点；独立运行缺省时按配置加载。类型定义仅出现在工具参数中，不再次追加到查询system。

---

## 查询与结果

查询使用 `classification.categories` nested，内部对 `classification.categories.code` 做term精确匹配，score_mode为none。一个合同可以属于多个类别，包含当前category即命中。父结果通过ES ID过滤限定；无效引用不回退全库。

与Core共用 `filter_execution.py`：PIT固定视图、每页500条、最多1000次请求，完整分页后按SQLite ready核验，再进入CandidatePool.filter。范围过大、超时、分片失败或引用中途失效返回错误，不提交部分结果；退出关闭PIT。

有父排名时保留原分数和顺序；首次纯筛选记1并标记无相关性排名。成功仅返回result_id与count，不返回第一页；零结果不创建引用。最终列表沿用统一结束工具与宿主分页机制。

同时满足A和B：筛A后引用其结果筛B。满足A或B：在同一初始范围独立查询，再使用union_contract_results；一路为空时直接选另一表。类型筛选只对入库分类负责，未分类合同不会命中，也不能据此证明合同具有某个具体条款。

---

## 验证

`tests/test_contract_category_search.py` 覆盖实际目录枚举、自定义类别动态变化、非法输入、nested查询、父范围和排名、零结果、失败不发布及子Agent查询后结束。共享执行器的多页、驱逐、权限与ready边界由Core回归测试覆盖。测试使用模拟ES与生成响应，未执行真实模型或ES联调。
