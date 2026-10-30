"""FastAPI 应用工厂与进程启动入口。"""

# 支持 IDE 直接运行 app/main.py，与 python -m app.main 使用相同导入路径。
if __name__ == "__main__" and not __package__:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os

import uvicorn
from fastapi import FastAPI

from app import __version__
from app.bootstrap import lifespan
from app.core.routing import APPLICATION_PREFIX, API_PREFIX
from app.router import router


def create_app() -> FastAPI:
    """创建供 ASGI 服务器和测试使用的应用实例。"""
    application = FastAPI(
        title="Contract Processor API",
        version=__version__,
        lifespan=lifespan,
        # 文档和业务接口共用应用前缀，避免多平台部署时根路径冲突。
        docs_url=f'{APPLICATION_PREFIX}/docs',
        redoc_url=f'{APPLICATION_PREFIX}/redoc',
        openapi_url=f'{APPLICATION_PREFIX}/openapi.json',
        swagger_ui_oauth2_redirect_url=f'{APPLICATION_PREFIX}/docs/oauth2-redirect',
    )
    application.include_router(router, prefix=API_PREFIX)
    return application


app = create_app()


if __name__ == "__main__":
    # 重载子进程继承开发前缀，API、接口文档和审核附件链接保持一致。
    os.environ["APP_PATH_PREFIX"] = "/dev"
    # IDE 开发入口：需要调整监听地址、端口或热重载时直接修改此处。
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=17000,
        log_level="info",
        reload=False,
    )
