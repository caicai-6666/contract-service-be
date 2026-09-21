"""备注全文索引及混合召回；只检索 ready 合同，排名单位先备注后合同。"""
from contextlib import closing
import math
import sqlite3

from app.infrastructure.sqlite_fts import connect_memory_database


def initialize_note_search(connection):
    """外部内容索引与触发器同事务创建；旧正文一次性回填，不生成旧向量。"""
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE name='contract_notes_fts'").fetchone()
    connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS contract_notes_fts USING fts5(content, content='contract_notes', content_rowid='rowid', tokenize='lindera_tokenizer')")
    for name, event, body in (
        ('insert', 'AFTER INSERT', 'INSERT INTO contract_notes_fts(rowid,content) VALUES(new.rowid,new.content);'),
        ('delete', 'AFTER DELETE', "INSERT INTO contract_notes_fts(contract_notes_fts,rowid,content) VALUES('delete',old.rowid,old.content);"),
        ('update', 'AFTER UPDATE OF content', "INSERT INTO contract_notes_fts(contract_notes_fts,rowid,content) VALUES('delete',old.rowid,old.content); INSERT INTO contract_notes_fts(rowid,content) VALUES(new.rowid,new.content);"),
    ):
        connection.execute(f'CREATE TRIGGER IF NOT EXISTS contract_notes_fts_{name} {event} ON contract_notes BEGIN {body} END')
    if not exists:
        connection.execute("INSERT INTO contract_notes_fts(contract_notes_fts) VALUES('rebuild')")


def retrieve_notes(database, query, embedding, scope, *, top_k=10):
    """一次读快照内完成两路召回，等权 RRF(k=60)，每份合同取最佳备注。"""
    if scope == ():
        return (), {}
    with closing(connect_memory_database(database)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('CREATE TEMP TABLE allowed(document_id TEXT PRIMARY KEY)')
        if scope is not None:
            connection.executemany('INSERT INTO allowed VALUES (?)', ((doc,) for doc in scope))
        connection.commit()
        connection.execute('BEGIN')
        where = "c.status='ready'"
        if scope is not None:
            where += ' AND n.document_id IN (SELECT document_id FROM allowed)'
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
                'SELECT n.note_id,n.document_id,bm25(contract_notes_fts) AS score FROM contract_notes_fts '
                'JOIN contract_notes n ON n.rowid=contract_notes_fts.rowid JOIN contracts c ON c.document_id=n.document_id '
                'WHERE contract_notes_fts MATCH ? AND '+where+' ORDER BY score,n.note_id LIMIT ?', (match,limit)).fetchall()
        vectors = connection.execute(
            'SELECT n.note_id,n.document_id,1-vec_distance_cosine(n.content_embedding,?) AS score '
            'FROM contract_notes n JOIN contracts c ON c.document_id=n.document_id WHERE '+where+
            ' AND n.content_embedding IS NOT NULL AND n.embedding_model=? AND n.embedding_version=? '
            'AND n.embedding_dimensions=? ORDER BY score DESC,n.note_id LIMIT ?',
            (embedding.to_blob(),embedding.model,embedding.version,len(embedding.vector),limit)).fetchall()
        scores, documents = {}, {}
        for lane in (lexical, vectors):
            for rank, row in enumerate(lane, 1):
                if not math.isfinite(row['score']):
                    raise ValueError('备注检索返回非法分数')
                note = row['note_id']
                documents[note] = row['document_id']
                scores[note] = scores.get(note,0.0) + 1/(60+rank)
        best = {}
        for note in sorted(scores, key=lambda n:(-scores[n],n)):
            best.setdefault(documents[note], (scores[note],note))
        ordered = sorted(best, key=lambda doc:(-best[doc][0],doc))[:top_k]
        return tuple((doc,best[doc][0]) for doc in ordered), {doc:best[doc][1] for doc in ordered}
