# Elasticsearch 本地开发部署

> **用途：** 本文说明如何用 Docker Compose 启动项目专用的单节点 Elasticsearch，以及应用连接、数据保留和安全边界。

本能力只服务本机开发与联调。正式环境必须单独配置访问控制、TLS、备份、容量和集群拓扑，不能直接复用这里关闭安全功能的容器参数。

---

## 部署结构

项目根目录的 `compose.yaml` 使用 Elastic 官方 `9.4.5` 镜像。该版本与 `requirements.txt` 中 `elasticsearch>=9.4,<10.0` 的 Python 客户端约束一致。

开发实例具有以下边界：

- 使用 `discovery.type=single-node`，不执行多节点发现。
- 关闭 Elasticsearch 安全认证与 HTTP TLS。
- 只把容器的 `9200` 端口映射到宿主机 `127.0.0.1`，不对局域网开放。
- 默认分配固定的 1 GB JVM 最小和最大堆。
- 使用命名卷 `contract-service-elasticsearch-data` 保存索引数据。
- 首次启动时安装官方 `analysis-smartcn` 插件，并使用独立插件卷保存安装结果。
- 通过集群健康接口执行容器健康检查，单节点无副本时 `yellow` 即可视为可用。

> **安全边界：** `xpack.security.enabled=false` 只允许用于绑定本机回环地址的开发实例。不得把端口映射修改为 `0.0.0.0` 后继续关闭认证。

---

## 启动与验证

首次启动会拉取约 900 MB 的官方镜像：

```bash
docker compose up -d elasticsearch
docker compose ps elasticsearch
curl --fail http://127.0.0.1:9200/
curl --fail 'http://127.0.0.1:9200/_cluster/health?wait_for_status=yellow&timeout=5s'
curl --fail 'http://127.0.0.1:9200/_cat/plugins?v'
```

应用使用以下开发连接参数：

```dotenv
ELASTICSEARCH_HOSTS=http://127.0.0.1:9200
ELASTICSEARCH_TEXT_ANALYZER=smartcn
```

应用客户端不接收用户名、密码或 CA 证书配置，直接通过 HTTP 访问 Compose 中关闭安全功能的节点。

---

## 配置项

| 配置 | 默认值 | 用途 |
| --- | --- | --- |
| `ELASTICSEARCH_VERSION` | `9.4.5` | Compose 使用的固定镜像版本。 |
| `ELASTICSEARCH_PORT` | `9200` | 绑定到宿主机回环地址的端口。 |
| `ELASTICSEARCH_JAVA_OPTS` | `-Xms1g -Xmx1g` | 开发实例 JVM 堆设置。 |
| `ELASTICSEARCH_HOSTS` | `http://127.0.0.1:9200` | Python 客户端访问地址。 |
| `ELASTICSEARCH_TEXT_ANALYZER` | `smartcn` | 合同中文全文字段在索引与查询时使用的分析器。 |

若本机 `9200` 已被占用，可以同时修改 `ELASTICSEARCH_PORT` 和 `ELASTICSEARCH_HOSTS` 中的端口。JVM 最小堆和最大堆应保持相同；内存紧张时可以同步降低，但需要重新验证合同向量索引和聚合负载。

---

## 生命周期与数据

停止容器但保留数据：

```bash
docker compose stop elasticsearch
docker compose start elasticsearch
```

删除容器但保留命名卷：

```bash
docker compose down
```

查看日志：

```bash
docker compose logs --follow elasticsearch
```

命名卷中的索引数据不会随 `docker compose down` 删除。只有明确不再需要本机开发数据时，才可以在确认卷名后执行 `docker compose down --volumes`；该操作不可从项目恢复索引内容。

---

## 依赖与限制

- Docker Engine 与 Docker Compose 必须可用。
- 宿主机至少应为 Elasticsearch 及其他开发服务预留足够内存。
- Compose 只负责运行 Elasticsearch；FastAPI 应用启动时负责探测并创建正式索引或补齐新增 Core mapping，但不会写入示例合同。
- 后续创建正式索引或实验索引时，所有 `text` 字段必须把 `analyzer` 和 `search_analyzer` 都设置为 `ELASTICSEARCH_TEXT_ANALYZER`。Core 字符串是否成为 `text` 由属性的 `tokenize` 配置决定，未启用时使用 `keyword`；条款标题和正文始终分词。正式合同字段边界见[合同 Elasticsearch 文档结构](../../architecture/data/contract-elasticsearch-document.md)。
- 应用启动会主动探测 Elasticsearch；开发实例未就绪、SmartCN 插件缺失或已有 Core mapping 不兼容时，API 不会开始接收请求。
- 启动同步只等待索引元数据确认，不等待活动分片；磁盘水位过高时索引仍可能为 `red`，应通过集群健康接口排查并释放宿主机空间，不能在应用中关闭 Elasticsearch 的磁盘保护。
- 正式索引与入库验收索引的隔离规则仍以[后端应用骨架](../application/backend-application.md#elasticsearch-边界)为准。
