# 版本控制与运行资源

本页说明后端哪些内容必须随源码分发，哪些由部署生成或作为业务数据保存。Git 仅允许部署必需内容、正式描述文档和必要目录占位，采用根目录 `.gitignore` 白名单约束；镜像构建输入由 `.dockerignore` 和 `Dockerfile` 定义，两者用途不同。

---

## 必须随源码保留

| 内容 | 用途 | Git 规则 |
| --- | --- | --- |
| `app/**/*.py` | HTTP、Agent、持久化、配置及基础设施实现，包括新增的共享 Embedding 指令 | 仅放行 Python 源码与父目录 |
| `requirements.txt` | Python 依赖声明 | 正常允许跟踪 |
| `Dockerfile`、`.dockerignore`、`.gitignore` | 构建、分发和运行数据隔离 | 正常允许跟踪 |
| `.env.example` | 可分发的环境变量契约，不含真实凭据 | 显式允许模板，忽略其他 `.env.*` |
| `config/lindera-jieba.yml` | 中文分词器运行配置 | 正常允许跟踪 |
| `config/lindera-Cargo.lock` | 构建 Lindera 的固定 Rust 依赖解析 | 正常允许跟踪，不使用全局 `*.lock` 忽略规则 |
| `data/definition/**/*.yaml`、`*.yml` | 合同类别、正负例、Core 字段和检索问题指南 | 在 `data` 默认忽略下放行 YAML 与父目录 |
| `data/tool-tag/*.txt` | 模型工具协议；是否读取由配置控制 | 按文件类型允许新增模型协议 |
| `data/template/*.jinja` | 模型部署侧聊天模板 | 按文件类型允许新增模板 |
| `README.md`、`AGENTS.md`、`description/**/*.md` | 运行入口、开发约定和功能契约 | 显式放行 Markdown 文档与父目录 |

配置中引用的文件必须存在于部署环境中。Git 允许新增文件不等于已经跟踪；新建源码和模板仍需在正常提交时显式加入，不能依赖 `.gitignore` 自动提交。

Jinja 文件主要由模型服务部署侧使用，后端镜像不因 Git 允许跟踪便自动复制它们。后端 Dockerfile 当前只复制应用、定义、工具协议、Python 依赖及 Lindera 构建配置；部署需要某项资源时应同时核对 Dockerfile 与 `.dockerignore`，不能只改 Git 规则。

`.dockerignore` 对业务定义 YAML、工具协议 TXT 和模型模板 Jinja 采用相同文件类型边界，最后排除嵌套凭据、系统文件和Python缓存。Dockerfile仍决定最终复制哪些资源；构建上下文允许某文件，不代表该文件会进入镜像。

Git 的父目录忽略会阻止子文件例外生效，因此 `.gitignore` 需显式放行配置目录。Docker 的目录例外可能连带放行后代，`.dockerignore` 直接声明文件模式，不照搬 `!data/` 等目录例外，避免把运行数据库和合同送入构建上下文。

---

## 运行必需但不提交的内容

| 内容 | 来源与保存方式 |
| --- | --- |
| `.env`、其他私有 `.env.*` | 部署时填写地址与凭据，参考 `.env.example` |
| Lindera 原生动态库与构建清单 | 本地在 `data/extensions/`；Dockerfile 构建适合容器的动态库和许可证至 `/opt/contract-service/extensions/` |
| 合同 PDF、SQLite 及 WAL/SHM | 运行时存入 `data/contract`、`data/abstract`、`data/communication` 等目录，需要持久卷和备份 |
| 入库、删除审核数据 | 运行时创建 `data/ingestion-review`、`data/deletion-review`，按后台服务保存和清理 |
| ES、Neo4j 数据 | 由外部服务及其持久卷管理，不属于后端源码 |

这些内容虽然运行时可能必需，但属于配置、平台相关构建产物或业务数据，应通过部署和备份交付。原生动态库不能用提交本机 macOS 文件的方式满足 Linux 镜像依赖，见[SQLite 中文检索依赖](sqlite-fts.md)。

`.gitkeep` 只表示空目录布局，不是程序启动依赖。目前仅保留 `data/abstract`、`data/contract`、`data/ingestion-review`、`data/ingestion-review/files`、`data/deletion-review` 五处既有占位。会话、审核、正式文件存储等组件会按需创建目录；不为其他运行目录补充占位。

---

## 仅保留在本机的辅助内容

按项目现有约定，`scripts/`、`tests/`、`experiment/`、`output/`、`tmp/` 继续忽略。部署构建不依赖这些目录。已有本机安装、迁移、重建脚本不能被当作克隆仓库必然拥有的功能；若某项脚本将来成为必需的交付工具，应另行迁移至正式目录并明确契约，而非整体放开所有本地脚本。

虚拟环境、Python 缓存、检查缓存、日志、IDE 配置和系统临时文件也不提交。`data` 下除明示的配置类型和目录占位外均默认忽略，即使目录已因占位放行，其中的数据库、合同、附件、备份和临时文件仍不应被纳入。

未列入白名单的根目录内容、应用目录内的非 Python 文件、描述目录内的非 Markdown 文件及额外配置默认忽略。若以后部署确实需要新的资源类型，应同步修改白名单、镜像复制范围和本文档。

通用私有文件与缓存规则位于资源白名单之后，防止 `.env.private.yaml`、`._definition.yaml` 等文件因为 YAML 扩展名被重新放行。环境模板例外只针对仓库根目录 `.env.example`。

---

## 检查方式

```bash
# 确认某文件具体命中了哪条忽略规则；--no-index也检查已跟踪文件。
git check-ignore -v --no-index data/communication/communication.db

# 查看当前尚未跟踪、但允许提交的文件。
git ls-files --others --exclude-standard

# 检查已经跟踪但现在被规则匹配的路径。
git ls-files -ci --exclude-standard

# 查看已分发的核心资源。
git ls-files app config data/definition data/template data/tool-tag
```

`.gitignore` 不会自动取消已经跟踪的文件。已经安排删除的旧资源应随对应业务改动提交；不能仅添加忽略规则就认为远程仓库已清理。修改规则时不应顺便删除工作目录、重置索引或改变已有暂存内容。

后端构建职责见[Docker 镜像](docker-deployment.md)，首次运行入口见[仓库 README](../../../README.md)。

---

## 本次验证范围

Git 白名单通过56个允许/忽略路径场景检查；检查时681个现存已跟踪文件均未被忽略，覆盖部署源码、配置、文档及目录占位。新增源码只是允许跟踪，仍须随对应功能正常提交。

Docker使用隔离的合成构建上下文、`FROM scratch` 和真实COPY进行导出检查：15个新增资源/私有文件场景通过，569个源码与目录资源和依赖清单按原字节复制，合同、数据库及嵌套凭据未进入导出。检查未联网，也未执行完整的Rust编译或应用镜像构建。
