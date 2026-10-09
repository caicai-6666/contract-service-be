# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SQLITE_LINDERA_EXTENSION_PATH=/opt/contract-service/extensions/liblindera_sqlite.so \
    LINDERA_CONFIG_PATH=/workspace/config/lindera-jieba.yml
WORKDIR /workspace
# 单阶段构建；依赖位于 data 卷外，重用旧数据卷时也能加载当前镜像的扩展。
ARG RUST_VERSION=1.98.1
ARG LINDERA_COMMIT=6aed060cc6af06edf36de983ad8eb8fc6e34bbd4
ARG LINDERA_SOURCE_SHA256=905e40df15c9a7ab4542f97e28675ed10b9027465fcd7d14a1e82a0e1b29b31a
COPY config/lindera-Cargo.lock /tmp/lindera-Cargo.lock
COPY config/lindera-jieba.yml ./config/lindera-jieba.yml
# 安装、编译与清理放在同一层；不依赖本地 scripts，也不复制 macOS 动态库。
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl build-essential libclang-dev pkg-config; \
    curl -fsSL --retry 3 https://sh.rustup.rs -o /tmp/rustup-init.sh; \
    CARGO_HOME=/tmp/cargo RUSTUP_HOME=/tmp/rustup sh /tmp/rustup-init.sh -y --profile minimal --default-toolchain "$RUST_VERSION" --no-modify-path; \
    curl -fL --retry 3 "https://codeload.github.com/lindera/lindera-sqlite/tar.gz/$LINDERA_COMMIT" -o /tmp/lindera.tar.gz; \
    echo "$LINDERA_SOURCE_SHA256  /tmp/lindera.tar.gz" | sha256sum -c -; \
    mkdir /tmp/lindera; \
    tar -xzf /tmp/lindera.tar.gz -C /tmp/lindera --strip-components=1; \
    cp /tmp/lindera-Cargo.lock /tmp/lindera/Cargo.lock; \
    cd /tmp/lindera; \
    CARGO_HOME=/tmp/cargo RUSTUP_HOME=/tmp/rustup PATH="/tmp/cargo/bin:$PATH" cargo build --locked --release --features embed-jieba; \
    mkdir -p /opt/contract-service/extensions; \
    cp target/release/liblindera_sqlite.so /opt/contract-service/extensions/; \
    cp LICENSE /opt/contract-service/extensions/LINDERA-LICENSE; \
    cd /workspace; \
    rm -rf /tmp/lindera /tmp/lindera.tar.gz /tmp/lindera-Cargo.lock /tmp/cargo /tmp/rustup /tmp/rustup-init.sh; \
    apt-get purge -y --auto-remove curl build-essential libclang-dev pkg-config; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./requirements.txt
RUN pip install -r requirements.txt
# 只复制运行代码、权威定义和工具格式模板，不将密钥、历史合同或 SQLite 烘焙进镜像。
COPY app ./app
# 构建时验证目标平台可以加载向量扩展；只使用内存库，不创建业务数据。
RUN python - <<'PY_CHECK'
from contextlib import closing
from app.infrastructure.sqlite_fts import connect_memory_database
with closing(connect_memory_database(':memory:')) as connection:
    connection.execute("CREATE VIRTUAL TABLE probe USING fts5(content, tokenize='lindera_tokenizer')")
    connection.executemany('INSERT INTO probe VALUES (?)', [('合同验收后支付尾款',), ('设备定期维护',)])
    rows = connection.execute("SELECT rowid, bm25(probe) FROM probe WHERE probe MATCH '验收'").fetchall()
    if len(rows) != 1 or rows[0][0] != 1 or rows[0][1] >= 0:
        raise RuntimeError('Lindera 中文 BM25 校验失败')
    print('Lindera 中文 BM25 与 sqlite-vec 验证通过：', connection.execute('SELECT vec_version()').fetchone()[0])
PY_CHECK
COPY data/definition ./data/definition
COPY data/tool-tag ./data/tool-tag
RUN mkdir -p data/contract data/abstract data/user
EXPOSE 20000
# 登录会话和提取任务驻留内存，必须单 worker，部署环境不启用 reload。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "20000", "--workers", "1"]
