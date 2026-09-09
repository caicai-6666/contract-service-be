"""仅供前后端样式联调的独立模拟服务，不启动正式后端生命周期。"""

import argparse
import math
import sys
from contextlib import asynccontextmanager
from tempfile import TemporaryDirectory
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import uvicorn
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.infrastructure.communication_store import SQLiteCommunicationStore
from app.service.communication_history import ConversationHistoryService
from app.router import auth, communication
from app.router.dependency import require_reviewer_user
from app.service.auth import AuthService, LoginCodeCache
from app.user import load_reviewer_user_catalog

from app.service.communication_demo import DemoEventService, SCENARIOS


def create_demo_app(*, user_file=None, allow_origins=(), **service_options):
    @asynccontextmanager
    async def lifespan(app):
        settings = get_settings()
        users = load_reviewer_user_catalog(user_file or settings.reviewer_user_path)
        app.state.auth_service = AuthService(
            reviewer_users=users,
            login_code_cache=LoginCodeCache(ttl_seconds=settings.auth_login_code_ttl_seconds),
        )
        service = DemoEventService(**service_options)
        app.state.communication_event_service = service
        # 独立联调服务使用临时会话库，不污染正式历史或持久化测试密钥。
        directory = TemporaryDirectory(prefix="communication-demo-")
        store = SQLiteCommunicationStore(Path(directory.name) / "communication.db")
        store.initialize()
        app.state.communication_store = store
        history = ConversationHistoryService(store)
        app.state.communication_history_service = history
        service.bind_history(history)
        await service.start()
        try:
            yield
        finally:
            await service.close()
            await history.close()
            directory.cleanup()

    app = FastAPI(title="Communication UI Demo — 仅供联调", lifespan=lifespan)
    app.include_router(auth.router, prefix="/contract/api")
    app.include_router(communication.router, prefix="/contract/api", dependencies=[Depends(require_reviewer_user)])
    if allow_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(allow_origins), allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type", "Last-Event-ID"])

    @app.get("/contract/api/health")
    async def health():
        return {"status": "ok", "mode": "communication-demo"}

    return app


def positive_float(raw):
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("必须为有限正数")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=20001)
    parser.add_argument("--user-file", type=Path)
    parser.add_argument("--scenario", choices=SCENARIOS, default="success")
    parser.add_argument("--delay", type=positive_float, default=0.2, help="每个文本块的间隔秒数")
    parser.add_argument("--slow-seconds", type=positive_float, default=60)
    parser.add_argument("--tool-seconds", type=positive_float, default=4, help="每个模拟工具的等待总秒数，不含文字输出")
    parser.add_argument("--activation-timeout", type=positive_float, default=180)
    parser.add_argument("--heartbeat", type=positive_float, default=3)
    parser.add_argument("--event-buffer-size", type=int, default=128)
    parser.add_argument("--allow-origin", action="append", default=[], help="允许直连的前端 Origin，可重复；默认不跨域")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or args.event_buffer_size <= 0:
        parser.error("端口或事件缓存大小无效")
    print("仅启动模拟联调服务：不连接 ES，不调用模型，不写正式合同；请使用此服务重新登录。")
    app = create_demo_app(
        user_file=args.user_file, allow_origins=args.allow_origin,
        scenario=args.scenario, delay=args.delay, slow_seconds=args.slow_seconds, tool_seconds=args.tool_seconds,
        activation_timeout_seconds=args.activation_timeout, heartbeat_seconds=args.heartbeat,
        event_buffer_size=args.event_buffer_size,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
