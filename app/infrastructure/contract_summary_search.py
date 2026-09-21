"""摘要全文索引与混合召回；只检索 ready 合同。"""
from contextlib import closing
import math
import sqlite3

from app.infrastructure.sqlite_fts import connect_memory_database


def initialize_summary_search(connection):
    """外部内容索引与触发器同事务创建；旧正文一次性回填，不生成旧向量。"""
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE name='contract_summaries_fts'").fetchone()
    connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS contract_summaries_fts USING fts5(summary, content='contracts', content_rowid='rowid', tokenize='lindera_tokenizer')")
    for name, event, body in (
        ('insert', 'AFTER INSERT', 'INSERT INTO contract_summaries_fts(rowid,summary) VALUES(new.rowid,new.summary);'),
        ('delete', 'AFTER DELETE', "INSERT INTO contract_summaries_fts(contract_summaries_fts,rowid,summary) VALUES('delete',old.rowid,old.summary);"),
        ('update', 'AFTER UPDATE OF summary', "INSERT INTO contract_summaries_fts(contract_summaries_fts,rowid,summary) VALUES('delete',old.rowid,old.summary); INSERT INTO contract_summaries_fts(rowid,summary) VALUES(new.rowid,new.summary);"),
    ):
        connection.execute(f'CREATE TRIGGER IF NOT EXISTS contract_summaries_fts_{name} {event} ON contracts BEGIN {body} END')
    if not exists:
        connection.execute("INSERT INTO contract_summaries_fts(contract_summaries_fts) VALUES('rebuild')")


def retrieve_summaries(database, query, embedding, scope, *, top_k=10):
    """一次读快照内完成两路召回，等权 RRF(k=60)，直接按合同排名。"""
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
        limit = min(top_k * 4, 200)
        lexical = []
        if terms:
            match = ' OR '.join('"'+term.replace('"','""')+'"' for term in terms)
            lexical = connection.execute(
                'SELECT c.document_id,bm25(contract_summaries_fts) AS score FROM contract_summaries_fts '
                'JOIN contracts c ON c.rowid=contract_summaries_fts.rowid '
                'WHERE contract_summaries_fts MATCH ? AND '+where+' ORDER BY score,c.document_id LIMIT ?', (match,limit)).fetchall()
        vectors = connection.execute(
            'SELECT c.document_id,1-vec_distance_cosine(c.summary_embedding,?) AS score '
            'FROM contracts c WHERE '+where+
            ' AND c.summary_embedding IS NOT NULL AND c.summary_embedding_model=? AND c.summary_embedding_version=? '
            'AND c.summary_embedding_dimensions=? ORDER BY score DESC,c.document_id LIMIT ?',
            (embedding.to_blob(),embedding.model,embedding.version,len(embedding.vector),limit)).fetchall()
        scores = {}
        for lane in (lexical, vectors):
            for rank, row in enumerate(lane, 1):
                if not math.isfinite(row['score']):
                    raise ValueError('摘要检索返回非法分数')
                doc = row['document_id']
                scores[doc] = scores.get(doc, 0.0) + 1/(60+rank)
        return tuple(sorted(scores.items(), key=lambda item:(-item[1],item[0]))[:top_k])
