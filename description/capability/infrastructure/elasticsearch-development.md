# Elasticsearch 部署与应用连接

> ES 的 Compose 编排和 SmartCN 镜像构建由独立 `contract-service-deploy` 项目维护；后端仓库不再保留 `compose.yaml`。

## 职责与启动

deploy 负责 ES 版本、插件、JVM、健康检查及持久化卷；后端负责启动时创建或检查索引 mapping、初始化 SQLite 和同步类别。无需额外初始化 SQL，不自动写入示例合同。

默认部署为单节点 ES 9.4.5。关闭认证的 ES 不发布宿主机端口，仅在专用 Compose 网络中访问；公网或多租户环境须另行配置认证、TLS 和网络隔离。

在 deploy 目录执行：

```bash
docker compose up -d --build elasticsearch
docker compose ps elasticsearch
docker compose exec elasticsearch curl -fsS 'http://localhost:9200/_cluster/health?wait_for_status=yellow&timeout=5s'
docker compose exec elasticsearch curl -fsS 'http://localhost:9200/_cat/plugins?v'
```

容器内后端使用 `ELASTICSEARCH_HOSTS=http://elasticsearch:9200`。宿主机直接运行后端时须另配可达地址，不能直接使用容器服务名。Compose 不再读取后端 `.env` 的版本、端口及 JVM 参数。

## 数据与限制

ES 使用 deploy 的独立命名卷。普通 `docker compose down` 保留数据，带 `--volumes` 会删除数据卷，除非明确要清空数据，否则不要使用。

移除旧 Compose 文件不会停止现存 `contract-service-elasticsearch` 容器，也不会删除旧 `contract-service-elasticsearch-data`、`contract-service-elasticsearch-plugins` 卷。本次未停止、删除或迁移它们。新部署不自动复用旧卷；历史合同须协调迁移 ES、SQLite 和 PDF，不能只迁移其中一项。

后端要求目标节点安装 `ELASTICSEARCH_TEXT_ANALYZER` 对应分析器，当前为 SmartCN。ES 不可达、插件缺失或 mapping 不兼容时启动失败。索引创建成功不保证分片可用；磁盘水位及节点启动检查应依据 ES 日志处理，不绕过保护。

正式数据契约见[合同 Elasticsearch 文档结构](../../architecture/data/contract-elasticsearch-document.md)，后端运行契约见[后端 Docker 镜像](docker-deployment.md)。
