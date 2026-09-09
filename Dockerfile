FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /workspace
COPY requirements.txt ./requirements.txt
RUN pip install -r requirements.txt
# 只复制运行代码、权威定义和工具格式模板，不将密钥、历史合同或 SQLite 烘焙进镜像。
COPY app ./app
# 构建时验证目标平台可以加载向量扩展；只使用内存库，不创建业务数据。
RUN python -c "from app.infrastructure.sqlite_vector import connect_vector_database; c = connect_vector_database(':memory:'); print(c.execute('SELECT vec_version()').fetchone()[0]); c.close()"
COPY data/definition ./data/definition
COPY data/tool-tag ./data/tool-tag
RUN mkdir -p data/contract data/abstract data/user
EXPOSE 20000
# 登录会话和提取任务驻留内存，必须单 worker，部署环境不启用 reload。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "20000", "--workers", "1"]
