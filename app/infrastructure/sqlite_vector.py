"""提供按连接加载的 SQLite 向量能力，不负责业务表和向量生成。"""

from pathlib import Path
import sqlite3

import sqlite_vec


def connect_vector_database(
    database: str | Path, *, timeout: float = 5.0
) -> sqlite3.Connection:
    """创建支持 sqlite-vec 的连接；调用方负责事务和关闭连接。"""
    connection = sqlite3.connect(database, timeout=timeout)
    try:
        # 仅在初始化期间允许加载随依赖安装的扩展，不接受外部扩展路径。
        connection.enable_load_extension(True)
        try:
            sqlite_vec.load(connection)
        finally:
            connection.enable_load_extension(False)
        connection.execute("SELECT vec_version()").fetchone()
    except BaseException:
        # 初始化失败不得泄漏连接，也不得静默退回不支持向量的连接。
        connection.close()
        raise
    return connection
