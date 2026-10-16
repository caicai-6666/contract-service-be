"""SQLite 通行证 JSON 编码及旧标量存储的一次性迁移。"""
import json
from app.core.passport import normalize_passports


def encode_passports(value) -> str | None:
    return None if value is None else json.dumps(normalize_passports(value), ensure_ascii=False, separators=(',', ':'))


def decode_passports(value: str | None) -> tuple[str, ...] | None:
    # 存储格式已由初始化迁移保证；禁止将损坏 JSON 猜测为历史单通行证。
    if value is None:
        return None
    decoded = json.loads(value)
    if not isinstance(decoded, list):
        raise ValueError('passport 存储必须为JSON数组')
    return normalize_passports(decoded)


def migrate_passport_storage(connection, table: str) -> None:
    """数据库版本0是旧标量，版本1起为JSON数组；与业务初始化处于同一事务。"""
    if table not in {'contracts', 'ingestion_reviews', 'deletion_reviews'}:
        raise ValueError('不支持的通行证存储表')
    # 空表也必须让版本号与后续初始化同事务提交；锁内重读版本避免并发重复包装。
    if not connection.in_transaction:
        connection.execute('BEGIN IMMEDIATE')
    if connection.execute('PRAGMA user_version').fetchone()[0] != 0:
        return
    # 逐行编码而不是判断值是否像JSON，避免误拆历史上恰好形似JSON的业务标识。
    for row in connection.execute(f'SELECT rowid,passport FROM {table} WHERE passport IS NOT NULL').fetchall():
        connection.execute(f'UPDATE {table} SET passport=? WHERE rowid=?', (encode_passports(row[1]), row[0]))
    connection.execute('PRAGMA user_version=1')
