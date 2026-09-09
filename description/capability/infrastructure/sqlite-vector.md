# SQLite 向量支持

## 用途与边界

通过固定版本 `sqlite-vec==0.1.9` 为 SQLite 连接提供向量距离函数及虚拟表能力。普通业务表可同时保存历史 JSON 和向量 BLOB，无需强制拆分向量表。

当前只提供连接能力，不创建 communication 历史表，不生成向量，不改变现有合同摘要库连接，也不改变 communication 的内存存储。后续向量化工具与历史存储另行实现。

---

## 使用方式

```python
from contextlib import closing
from sqlite_vec import serialize_float32
from app.infrastructure.sqlite_vector import connect_vector_database

with closing(connect_vector_database(":memory:")) as connection:
    vector = serialize_float32([1.0, 0.0])
    distance = connection.execute(
        "SELECT vec_distance_cosine(?, ?)", (vector, vector)
    ).fetchone()[0]
```

`connect_vector_database(database, timeout=5.0)` 返回标准 `sqlite3.Connection`，支持文件路径或 `:memory:`。调用方负责创建父目录、事务提交及关闭；函数不自动设置 WAL、外键或行工厂，不接受用户指定的扩展路径。扩展逐连接加载，加载后立即关闭扩展加载权限，初始化失败关闭连接并向调用方抛出异常。

普通表可使用 `(user_id, created_at)` 索引筛选候选，再通过 `vec_distance_cosine` 排序取前若干条；这是候选范围内的精确遍历，不是 ANN 索引。业务层仍须强制限定当前登录用户、排除空向量，并保证向量模型、维度、非零及有限数值等约束一致。

---

## 安装与验证

使用项目 Python 环境执行 `python -m pip install -r requirements.txt`。依赖采用固定版本，升级时重新验证查询行为及目标平台兼容性。

Dockerfile 在镜像构建时通过内存数据库验证扩展加载，失败则停止构建；不会修改挂载的数据。已有容器需要重新构建和部署镜像才能包含新依赖，不会自动更新。

Python 所链接的 SQLite 必须支持扩展加载；部分系统自带 Python 不支持此能力。连接加载失败时应更换支持扩展的 Python 发行版，不能静默绕过。使用方法参见 [sqlite-vec Python 文档](https://alexgarcia.xyz/sqlite-vec/python.html)，普通表查询参见 [KNN 文档](https://alexgarcia.xyz/sqlite-vec/features/knn.html)。

本地测试覆盖实际加载、普通表 BLOB 距离计算、用户与时间过滤，以及初始化失败后的连接释放和扩展加载权限关闭。测试位于本地 `tests/test_sqlite_vector.py`，遵循项目现有测试目录不追踪约定。
