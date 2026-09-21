# SQLite 中文全文检索依赖

为记忆检索准备 Lindera FTS5 原生扩展，采用内嵌 Jieba 词典。原文和查询由同一分词器切分，SQLite 提供 BM25 排序；向量能力继续复用 [sqlite-vec](sqlite-vector.md)。

当前已完成本机安装、配置与连接入口。尚未创建业务 FTS 表、同步历史索引或接入[记忆检索子图](../../architecture/workflow/contract-communication/memory-retrieval.md)。原有 CommunicationStore 仍使用原连接，不会因本次准备工作改变读写行为。

---

## 依赖与安装

Lindera 是原生动态库，不通过 `requirements.txt` 安装。官方 v2.0.0 发布包的中文词典是 CC-CEDICT；本项目需要 Jieba，因此固定上游源码提交 `6aed060cc6af06edf36de983ad8eb8fc6e34bbd4`，启用 `embed-jieba` 编译。不能仅凭源码包仍标记为2.0.0就替换成同版本发布二进制。

- [固定源码](https://github.com/lindera/lindera-sqlite/tree/6aed060cc6af06edf36de983ad8eb8fc6e34bbd4)及源码SHA256由 `scripts/install_lindera.py` 固定。
- 上游未提交依赖锁，本项目保存 `config/lindera-Cargo.lock`，安装时使用 `cargo build --locked`。当前解析到 Lindera 5.3.0。
- 构建需要 Rust/Cargo 和本机C/C++工具链；本机使用 Rust 1.98.1、macOS ARM64。Rust只在构建时使用，运行时不需要。
- 安装脚本目前支持macOS和Linux；Python需3.12及以上并支持SQLite扩展加载。Linux还需C编译工具及libclang供上游绑定构建使用。

```bash
python scripts/install_lindera.py
python scripts/check_lindera.py
```

网络代理不可用时，可显式用 `python scripts/install_lindera.py --direct`，只对该安装进程绕过代理，不修改系统配置。

动态库保存在 `data/extensions/lindera/`，同时保留扩展许可证和 `manifest.json`，记录源码提交、源码校验值、平台、构建特性与动态库校验值。源码、构建缓存位于 `output/lindera-install/`，均不进入Git。配置及安装脚本进入版本控制。

当前Dockerfile尚未增加Lindera构建阶段；部署Linux时应在目标平台构建，不能将本机 `.dylib` 复制到Linux镜像。

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
