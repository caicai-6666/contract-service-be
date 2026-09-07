# 后端 Docker 镜像

> 后端仓库仅维护自身 Dockerfile 与构建上下文；服务编排、环境注入、用户配置和数据卷由独立 `contract-service-deploy` 项目管理。

## 构建与职责

在后端根目录执行 `docker build -t contract-service-be:local .`。镜像使用 Python 3.12，安装 `requirements.txt`，仅复制 `app/` 和 `data/definition/`；`.dockerignore` 排除密钥、用户配置、合同文件、数据库及其他非运行文件。

容器工作目录为 `/workspace`，Python 包位于 `/workspace/app`，数据位于 `/workspace/data`。启动命令为 `uvicorn app.main:app --host 0.0.0.0 --port 20000 --workers 1`。内存登录态与提取任务要求单 worker，不启用热重载；重启会清空登录态及未入库任务。

## 部署契约

- 注入模型服务及 Elasticsearch 连接环境变量；容器中的 localhost 不是宿主机。
- 通过 `backend-data` 命名卷整体持久化 `/workspace/data`，包含定义、PDF、SQLite（包括 WAL/SHM）及未来新增的数据子目录。首次空卷默认从镜像填充；已有非空卷不会随镜像升级自动刷新定义，须备份后显式更新对应定义并重建后端容器，不能清空整个卷。
- 顶层用户配置以只读文件挂载到 `/workspace/data/user/users.yaml`，也可通过 `REVIEWER_USER_FILE` 指定其他路径。文件结构遵循[审核用户 YAML 定义](../../architecture/data/reviewer-user-definition.md)。
- Elasticsearch 就绪后启动后端，由应用校验索引、初始化 SQLite 和同步类别；详见[Elasticsearch 部署边界](elasticsearch-development.md)。
- 健康检查路径为 `/contract/api/health`，不代表外部模型服务可用。
- deploy 的前后端共用 `BIND_ADDRESS` 配置宿主机监听地址，分别通过 `FRONTEND_PORT` 和 `BACKEND_PORT` 配置宿主机端口；后端映射到容器固定端口 20000。外部代理须保留 `/contract/api/` 路径与鉴权头，支持 SSE，并限制宿主机端口的访问来源。

Compose 入口为同级部署项目的 `docker-compose.yml`，构建上下文指向本仓库，使用根目录 `Dockerfile`。本仓库不再维护 `compose.yaml`。环境文件或用户配置更新后须按部署说明重新创建容器，不能仅依靠热加载。

## 验证与限制

构建后可用 `docker run --rm --network none contract-service-be:local python -c 'from app.main import app; app.openapi()'` 检查应用导入。完整启动及模型调用还需有效用户配置、ES 和模型服务；导入检查不能替代端到端验证。

基础镜像标签及 Python 依赖范围并非完整锁定文件，正式发布应固定验证过的镜像摘要和依赖版本。删除旧 Compose 配置不会停止已有开发容器，也不会删除旧 ES 数据卷；旧数据不自动迁入新部署。
