"""关系候选混合排名；Neo4j 是唯一持久化来源，SQLite 仅为查询内存计算。"""
from contextlib import closing
import math
import struct

from app.infrastructure.sqlite_fts import connect_memory_database
from app.service.contract_relation_embedding import RELATION_EMBEDDING_VERSION


def rank_relations(rows, query, embedding, *, top_k):
    with closing(connect_memory_database(':memory:')) as c:
        c.execute("CREATE VIRTUAL TABLE docs USING fts5(content, tokenize='lindera_tokenizer')")
        c.executemany('INSERT INTO docs(rowid,content) VALUES (?,?)',[(i+1,r['description']) for i,r in enumerate(rows)])
        c.execute("CREATE VIRTUAL TABLE terms USING fts5(content, tokenize='lindera_tokenizer')")
        c.execute('INSERT INTO terms VALUES (?)',(query,))
        c.execute("CREATE VIRTUAL TABLE vocabulary USING fts5vocab(terms,'row')")
        terms=[r[0] for r in c.execute('SELECT term FROM vocabulary ORDER BY term') if any(x.isalnum() for x in r[0])]
        limit=min(top_k*4,200)
        lexical=[]
        if terms:
            match=' OR '.join('"'+t.replace('"','""')+'"' for t in terms)
            lexical=c.execute('SELECT rowid FROM docs WHERE docs MATCH ? ORDER BY bm25(docs),rowid LIMIT ?',(match,limit)).fetchall()
        c.execute('CREATE TABLE vectors(id INTEGER PRIMARY KEY, embedding BLOB)')
        for i,r in enumerate(rows,1):
            v=r.get('description_embedding')
            if v is None or r.get('embedding_model') != embedding.model or r.get('embedding_version') != RELATION_EMBEDDING_VERSION or r.get('embedding_dimensions') != len(embedding.vector):
                continue
            if len(v)!=len(embedding.vector) or any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) for x in v) or not math.isfinite(math.hypot(*v)) or math.hypot(*v)==0:
                raise ValueError('关系向量无效')
            c.execute('INSERT INTO vectors VALUES (?,?)',(i,struct.pack(f'<{len(v)}f',*v)))
        vector=c.execute('SELECT id FROM vectors ORDER BY vec_distance_cosine(embedding,?),id LIMIT ?',
            (struct.pack(f'<{len(embedding.vector)}f',*embedding.vector),limit)).fetchall()
        scores={}
        for lane in (lexical,vector):
            for rank,(i,) in enumerate(lane,1):
                rid=rows[i-1]['relation_id'];scores[rid]=scores.get(rid,0)+1/(60+rank)
        return tuple(sorted(scores.items(),key=lambda x:(-x[1],x[0]))[:top_k])
