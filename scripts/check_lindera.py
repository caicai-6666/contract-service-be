"""使用内存库检查真实Jieba分词、BM25、向量共存及重新连接。"""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.infrastructure.sqlite_fts import connect_memory_database


def main():
    for _ in range(2):
        with closing(connect_memory_database(':memory:')) as connection:
            connection.execute("CREATE VIRTUAL TABLE probe USING fts5(content, tokenize='lindera_tokenizer')")
            connection.executemany('INSERT INTO probe(content) VALUES (?)', [
                ('合同验收完成后支付尾款。',), ('设备需要定期维护。',),
            ])
            rows = connection.execute('SELECT rowid, bm25(probe) FROM probe WHERE probe MATCH ? ORDER BY bm25(probe)', ('验收',)).fetchall()
            if len(rows) != 1 or rows[0][0] != 1 or rows[0][1] >= 0:
                raise RuntimeError(f'中文BM25查询未通过：{rows}')
            connection.execute("CREATE VIRTUAL TABLE vocabulary USING fts5vocab(probe, 'row')")
            terms = [row[0] for row in connection.execute('SELECT term FROM vocabulary ORDER BY term')]
            if not {'合同', '验收'}.issubset(terms):
                raise RuntimeError(f'中文词语切分未通过：{terms}')
            connection.execute('SELECT vec_version()').fetchone()
            try:
                connection.execute("SELECT load_extension('forbidden')")
            except sqlite3.OperationalError as exc:
                if 'not authorized' not in str(exc):
                    raise
            else:
                raise RuntimeError('扩展加载权限未关闭')
    print('通过：中文分词、BM25命中、sqlite-vec共存、重新连接、加载权限关闭。')
    print('分词示例：', terms)


if __name__ == '__main__':
    main()
