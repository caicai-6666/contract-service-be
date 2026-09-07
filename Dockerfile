FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /workspace
COPY requirements.txt ./requirements.txt
RUN pip install -r requirements.txt
# 只复制运行代码和权威定义，不将密钥、历史合同或 SQLite 烘焙进镜像。
COPY app ./app
COPY data/definition ./data/definition
RUN mkdir -p data/contract data/abstract data/user
EXPOSE 20000
# 登录会话和提取任务驻留内存，必须单 worker，部署环境不启用 reload。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "20000", "--workers", "1"]
