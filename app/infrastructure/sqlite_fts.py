"""为未来记忆检索提供Lindera连接；不建业务索引、不修改现有存储连接。"""
import os
from pathlib import Path
import sqlite3
import sys
from threading import Lock

from app.core.config import get_settings
from app.infrastructure.sqlite_vector import connect_vector_database

_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_LOCK = Lock()
_active_config: Path | None = None


def _absolute(path: Path) -> Path:
    return (path if path.is_absolute() else _ROOT / path).resolve()


def load_lindera(connection: sqlite3.Connection) -> None:
    """加载固定配置的分词器；同进程禁止切换配置，避免索引与查询切分不一致。"""
    global _active_config
    settings = get_settings()
    config = _absolute(settings.lindera_config_path)
    library = _absolute(settings.sqlite_lindera_extension_path)
    if not library.is_file():
        suffix = '.dylib' if sys.platform == 'darwin' else '.dll' if sys.platform == 'win32' else '.so'
        library = Path(str(library) + suffix)
    if not config.is_file() or not library.is_file():
        raise RuntimeError('Lindera扩展或配置不存在，请运行scripts/install_lindera.py并检查路径配置。')
    with _CONFIG_LOCK:
        if _active_config is not None and _active_config != config:
            raise RuntimeError('Lindera配置不能在同一进程内切换，请重启服务。')
        # 上游在创建tokenizer时读取进程环境；统一绝对路径，不随工作目录漂移。
        os.environ['LINDERA_CONFIG_PATH'] = str(config)
        connection.enable_load_extension(True)
        try:
            connection.load_extension(str(library), entrypoint='lindera_fts5_tokenizer_init')
        finally:
            connection.enable_load_extension(False)
        _active_config = config


def connect_memory_database(database: str | Path, *, timeout: float = 5.0) -> sqlite3.Connection:
    """返回同时具有sqlite-vec与Lindera FTS5能力的连接，由调用方关闭。"""
    connection = connect_vector_database(database, timeout=timeout)
    try:
        load_lindera(connection)
    except BaseException:
        connection.close()
        raise
    return connection
