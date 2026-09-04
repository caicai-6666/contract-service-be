"""FastAPI 应用工厂与进程启动入口。"""

import uvicorn
from fastapi import FastAPI

from app import __version__
from app.bootstrap import lifespan
from app.router import router


def create_app() -> FastAPI:
    """创建供 ASGI 服务器和测试使用的应用实例。"""
    application = FastAPI(
        title="Contract Processor API",
        version=__version__,
        lifespan=lifespan,
    )
    application.include_router(router, prefix="/contract/api")
    return application


app = create_app()


if __name__ == "__main__":
    # IDE 开发入口：需要调整监听地址、端口或热重载时直接修改此处。
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=20000,
        log_level="info",
        reload=True,
    )
