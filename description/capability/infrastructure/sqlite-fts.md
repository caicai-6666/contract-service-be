# SQLite 中文全文检索依赖

Lindera FTS5 原生扩展采用内嵌 Jieba 词典，为会话记忆与合同名称、摘要、注意事项提供中文检索。原文和查询由同一分词器切分，SQLite 提供 BM25 排序；向量能力继续复用 [sqlite-vec](sqlite-vector.md)。

当前已接入[记忆检索子图](../../architecture/workflow/contract-communication/memory-retrieval.md)：读取筛选后的任务检索投影，在内存 FTS 表中进行 BM25。正式合同的名称、摘要和注意事项使用持久化 FTS 表及同步触发器；业务库初始化与检索连接均需可用的扩展。CommunicationStore 的普通持久化连接继续加载 sqlite-vec，不在归档写入时构建记忆 FTS 表。

---

## 依赖与安装

Lindera 是原生动态库，不通过 `requirements.txt` 安装。官方 v2.0.0 发布包的中文词典是 CC-CEDICT；本项目需要 Jieba，因此固定上游源码提交 `6aed060cc6af06edf36de983ad8eb8fc6e34bbd4`，启用 `embed-jieba` 编译。不能仅凭源码包仍标记为2.0.0就替换成同版本发布二进制。

- [固定源码](https://github.com/lindera/lindera-sqlite/tree/6aed060cc6af06edf36de983ad8eb8fc6e34bbd4)及源码 SHA256 由根目录 `Dockerfile` 固定。
- 上游未提交依赖锁，本项目保存 `config/lindera-Cargo.lock`，安装时使用 `cargo build --locked`。当前解析到 Lindera 5.3.0。
- 构建需要 Rust/Cargo 和本机C/C++工具链；本机使用 Rust 1.98.1、macOS ARM64。Rust只在构建时使用，运行时不需要。
- 安装脚本目前支持macOS和Linux；Python需3.12及以上并支持SQLite扩展加载。Linux还需C编译工具及libclang供上游绑定构建使用。

以下命令仅适用于本机已有辅助脚本的开发环境；新部署使用下述 Docker 构建。

```bash
python scripts/install_lindera.py
python scripts/check_lindera.py
```

网络代理不可用时，可显式用 `python scripts/install_lindera.py --direct`，只对该安装进程绕过代理，不修改系统配置。

动态库保存在 `data/extensions/lindera/`，同时保留扩展许可证和 `manifest.json`，记录源码提交、源码校验值、平台、构建特性与动态库校验值。源码、构建缓存位于 `output/lindera-install/`，均不进入Git。配置和 Cargo 依赖锁进入版本控制；`scripts/` 仅本地保留，克隆项目不会获得这些辅助脚本。

### Docker 部署

根目录 Dockerfile 采用单阶段构建，直接安装编译工具和固定 Rust 1.98.1，下载并校验固定源码，使用项目 Cargo.lock 执行 `cargo build --locked --release --features embed-jieba`。不依赖 `scripts/`，不复制宿主机的动态库。编译完成后在同一层清理源码、Rust 工具链和临时构建依赖。

Linux 扩展及许可证放在 `/opt/contract-service/extensions/`，分词配置放在 `/workspace/config/lindera-jieba.yml`。镜像通过环境变量设置这两个路径，deploy 的 Compose 同步固定容器路径，避免本机 `.env` 的开发路径覆盖它们。扩展位于 `/workspace/data` 持久化卷之外，已有业务数据卷不会遮住更新后的扩展。

`.dockerignore` 仅放行分词配置和 Cargo 锁文件。镜像构建末尾通过应用实际连接入口验证中文 BM25 和 sqlite-vec，检查失败会阻止镜像构建完成。

```bash
docker build -t contract-service-be:local .
```

首次构建需要访问 Debian、Rust、GitHub、Cargo 和词典下载源，耗时比后续缓存构建长。部署主机无需安装 Rust；不同 CPU 架构需各自构建适配的镜像。

---

## 配置与连接

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `SQLITE_LINDERA_EXTENSION_PATH` | `data/extensions/lindera/liblindera_sqlite` | 原生扩展路径，省略后缀时按平台补齐。 |
| `LINDERA_CONFIG_PATH` | `config/lindera-jieba.yml` | 进程共用的中文分词配置。 |

相对路径统一按项目根目录解析。配置使用 `embedded://jieba`、normal模式、不保留空白及NFKC字符规范化；未设置停用词或业务词典，标点目前也可能成为词项。后续查询构造需要处理这些词项，不能视作已完成召回质量优化。

```python
from contextlib import closing
from app.infrastructure.sqlite_fts import connect_memory_database

with closing(connect_memory_database(':memory:')) as connection:
    connection.execute(
        "CREATE VIRTUAL TABLE example USING fts5(content, tokenize='lindera_tokenizer')"
    )
    connection.execute('INSERT INTO example(content) VALUES (?)', ('合同验收后支付尾款',))
    rows = connection.execute(
        'SELECT content, bm25(example) FROM example WHERE example MATCH ? ORDER BY bm25(example)',
        ('验收',),
    ).fetchall()
```

`connect_memory_database()` 返回同时支持sqlite-vec和Lindera的标准SQLite连接，由调用方关闭、管理事务和建表。也可对已有连接调用 `load_lindera()`。每个连接单独加载扩展，随后关闭扩展加载权限；加载失败不会静默退回默认分词器。

上游在创建tokenizer时读取进程环境。加载入口统一设置配置绝对路径，同进程禁止切换到其他配置路径；运行期间不得直接修改配置文件。更换词典或分词配置后需重启进程并重建已有FTS索引，不能混用不同规则生成的索引。

SQLite FTS5的BM25分数越小越靠前，与后续RRF分数方向不同；子图中转换排名时需要保持这一边界。

---

## 已验证范围

`check_lindera.py` 仅使用内存库，验证真实中文词语切分、BM25命中与分数方向、sqlite-vec共存、重复建立连接及加载后权限关闭。本机已通过，示例词项包括“合同”“验收”“尾款”“支付”。

这些检查证明扩展可用，不代表公司名称、编号、长内容召回准确率已完成评估。没有调用模型或改动真实会话数据。
