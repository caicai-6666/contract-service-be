# Neo4j 部署与开发连接

Neo4j 为后续合同关联和限定深度的邻域查询提供图存储。已安装官方 Python 驱动、连接配置及通用异步客户端 `Neo4jClient`；已接入应用生命周期及合同入库、删除和启动恢复；关系创建、独立删除及一跳查询接口已实现，Agent 图检索工具尚未实现。

---

## 配置与启动

与 Elasticsearch 一致，由同级 `contract-service-deploy` 维护 `neo4j/Dockerfile`、主 `docker-compose.yml`、`.env.example` 和开发端口覆盖 `compose.neo4j-dev.yml`。完整操作见 [deploy 部署说明](../../../../contract-service-deploy/README.md#neo4j-部署与本地开发)。

默认版本为 Neo4j Community `5.26.30-community`。主 Compose 仅允许内部网络访问；开发覆盖文件固定监听回环地址 HTTP 7474、Bolt 7687。与 ES 一致关闭认证（`NEO4J_AUTH: "none"`），无需用户名和密码。宿主机后续驱动使用 `bolt://127.0.0.1:7687`；容器后端使用 `bolt://neo4j:7687`。

在 deploy 目录执行：

```bash
docker compose -f docker-compose.yml -f compose.neo4j-dev.yml up -d --build --wait neo4j
docker compose -f docker-compose.yml -f compose.neo4j-dev.yml ps neo4j
```

仅操作 Neo4j，不依赖模型服务，不启动其他应用。健康检查执行无认证的 Bolt 查询；容器运行不等于健康检查已通过。

---

## 数据与边界

使用 `neo4j-data` 和 `neo4j-logs` 命名卷，实际卷名带 Compose 项目前缀。重启和重建容器保留数据，不使用 `down --volumes`。关闭认证不删除已有数据库或认证数据。

初始堆 256 MiB、最大堆 512 MiB、页面缓存 256 MiB、容器内存上限 2 GiB，均由 deploy 环境变量控制。部署不安装 APOC 或图算法插件，不写入业务或示例数据；合同仍以现有 SQLite、ES 和 PDF 存储为准。合同删除已通过 SQLite `deleting` 状态支持请求重试和启动补偿；备份策略仍需覆盖各存储。


---

## 后端依赖与环境变量

`requirements.txt` 声明官方驱动 `neo4j>=6.0,<7.0`，支持 `AsyncGraphDatabase` 异步连接。本项目 `.env` 和 `.env.example` 提供以下参数，`app.core.config.get_settings()` 读取并缓存为 `Settings` 字段：

| 环境变量 | 配置字段 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `NEO4J_URI` | `neo4j_uri` | `bolt://127.0.0.1:7687` | 宿主机连接入口 |
| `NEO4J_DATABASE` | `neo4j_database` | `neo4j` | 查询目标数据库 |
| `NEO4J_CONNECTION_TIMEOUT_SECONDS` | `neo4j_connection_timeout_seconds` | `30` | 建立连接超时，必须大于零，不是查询超时 |

deploy 的后端服务将 `NEO4J_URI` 覆盖为 `bolt://neo4j:7687`，避免使用容器自身的 localhost。当前无认证，创建驱动使用 `auth=None`；不定义账号、密码或认证开关。应用启动时检查连接、初始化合同身份唯一约束并恢复合同同步状态；Neo4j 不可用会阻止启动，修改环境变量后需重新加载配置或重启应用。


---

## 异步操作基础设施

实现位于 `app/infrastructure/neo4j.py`，`Neo4jClient(settings)` 创建无认证官方异步驱动并拥有其连接池。也可通过 `driver=` 注入测试驱动，注入后仍由客户端负责关闭。

| 方法 | 职责 |
| --- | --- |
| `verify_connectivity()` | 检查连接，并在配置的目标数据库执行 `RETURN 1` |
| `query(cypher, parameters, mode="read")` | 单条参数化查询；写入必须显式指定 `mode="write"`；返回官方 `EagerResult`，包含 records、summary、keys |
| `execute_read(work)` | 在独立会话中执行托管读事务回调 |
| `execute_write(work)` | 在独立会话中执行托管写事务回调，异常回滚 |
| `close()` | 释放连接池，可重复调用；关闭后拒绝查询 |
| `async with` | 退出时自动关闭，内部异常仍向调用方传播 |

示例：

```python
from app.core.config import get_settings
from app.infrastructure.neo4j import Neo4jClient

async def check_graph():
    async with Neo4jClient(get_settings()) as client:
        await client.verify_connectivity()
        result = await client.query("RETURN $value AS value", {"value": "连接成功"})
        return result.records[0]["value"]
```

同一事件循环内可复用客户端，每个并发操作拥有独立 session；会话显式指定目标数据库，共用驱动的 bookmark manager 维护跨会话因果一致性。正式运行时应由组合根创建一个共享实例，停止在途操作后再关闭；已由 `bootstrap` 创建共享实例并在关闭或启动失败时释放。

单条查询在事务内完整消费结果，避免关闭会话后才访问懒游标；结果全部驻留内存，业务查询须限制规模。多语句回调接收官方 `AsyncManagedTransaction`，同样必须在回调内消费结果，不能返回 `AsyncResult`。读取模式是路由意图，不是安全权限隔离。

官方驱动处理可重试的瞬时故障，封装不再叠加重试，也不把异常转成空列表。托管事务回调可能重复执行，必须适合重试，不能包含模型调用、SQLite 写入或文件修改；这些副作用不属于 Neo4j 原子事务。参数值通过字典传入，禁止将用户输入拼接为 Cypher。

验证覆盖独立并发会话、参数传递、目标数据库、异常释放及关闭后拒绝复用；合同同步另覆盖状态迁移、失败重试和启动恢复。真实 Neo4j 与临时 SQLite/PDF 联调已验证节点幂等及关联边删除，ES 使用隔离替身；本地真实 Neo4j 已验证写入、并发读取及失败事务回滚，临时探针节点执行后清除。


---

## 合同节点同步

`ContractGraphStore` 位于 `app/infrastructure/contract_graph_store.py`，提供 `initialize()`、`ensure_contract(document_id)`、`delete_contract(document_id)`。节点标签为 `Contract`，仅存完整 `document_id`，使用唯一约束和 `MERGE` 去重；删除使用 `DETACH DELETE` 同事务移除全部入边与出边，不删除相邻合同节点。

节点只在正式入库流程同步，上传和提取阶段不创建。入库前后的状态、历史节点补建和失败恢复由[合同正式入库](../application/contract-ingestion.md)统一定义。部署后端已声明 Neo4j 健康依赖；初始化不创建虚构合同或关系。

关系边的字段及不可修改、删除重建规则，以[合同关联图存储契约](../../architecture/data/contract-graph.md)为准；当前节点生命周期、关系创建及独立删除已接入业务。
