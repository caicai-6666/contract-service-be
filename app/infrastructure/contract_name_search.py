"""名称全文索引与 BM25 召回；只检索 ready 合同。"""
from contextlib import closing
import math
import sqlite3

from app.infrastructure.sqlite_fts import connect_memory_database


def initialize_name_search(connection):
    """外部内容索引与触发器同事务创建；旧正文一次性回填，不生成旧向量。"""
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE name='contract_names_fts'").fetchone()
    connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS contract_names_fts USING fts5(file_name, content='contracts', content_rowid='rowid', tokenize='lindera_tokenizer')")
    for name, event, body in (
        ('insert', 'AFTER INSERT', 'INSERT INTO contract_names_fts(rowid,file_name) VALUES(new.rowid,new.file_name);'),
        ('delete', 'AFTER DELETE', "INSERT INTO contract_names_fts(contract_names_fts,rowid,file_name) VALUES('delete',old.rowid,old.file_name);"),
        ('update', 'AFTER UPDATE OF file_name', "INSERT INTO contract_names_fts(contract_names_fts,rowid,file_name) VALUES('delete',old.rowid,old.file_name); INSERT INTO contract_names_fts(rowid,file_name) VALUES(new.rowid,new.file_name);"),
    ):
        connection.execute(f'CREATE TRIGGER IF NOT EXISTS contract_names_fts_{name} {event} ON contracts BEGIN {body} END')
    if not exists:
        connection.execute("INSERT INTO contract_names_fts(contract_names_fts) VALUES('rebuild')")


def retrieve_names(database, query, scope, *, top_k=10):
    """只匹配合同名称，按 BM25 降序返回；不编码、不融合历史分数。"""
    if scope == ():
        return ()
    with closing(connect_memory_database(database)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('CREATE TEMP TABLE allowed(document_id TEXT PRIMARY KEY)')
        if scope is not None:
            connection.executemany('INSERT INTO allowed VALUES (?)', ((doc,) for doc in scope))
        connection.commit()
        connection.execute('BEGIN')
        where = "c.status='ready'"
        if scope is not None:
            where += ' AND c.document_id IN (SELECT document_id FROM allowed)'
        # 用同一分词器取得词项，逐项引用，用户文本不会作为 FTS 运算符执行。
        connection.execute("CREATE VIRTUAL TABLE temp.query_terms USING fts5(content, tokenize='lindera_tokenizer')")
        connection.execute('INSERT INTO query_terms VALUES (?)', (query,))
        connection.execute("CREATE VIRTUAL TABLE temp.query_vocab USING fts5vocab(query_terms, 'row')")
        terms = [row[0] for row in connection.execute('SELECT term FROM query_vocab ORDER BY term') if any(c.isalnum() for c in row[0])]
        limit = top_k
        lexical = []
        if terms:
            match = ' OR '.join('"'+term.replace('"','""')+'"' for term in terms)
            lexical = connection.execute(
                'SELECT c.document_id,bm25(contract_names_fts) AS score FROM contract_names_fts '
                'JOIN contracts c ON c.rowid=contract_names_fts.rowid '
                'WHERE contract_names_fts MATCH ? AND '+where+' ORDER BY score,c.document_id LIMIT ?', (match,limit)).fetchall()
        hits = []
        for row in lexical:
            # SQLite bm25 越小越好；取负以统一结果池的降序契约。
            score = -float(row['score'])
            if not math.isfinite(score):
                raise ValueError('名称检索返回非法分数')
            hits.append((row['document_id'],score))
        return tuple(hits)
