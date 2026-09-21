"""候选快照→字段并发召回→LlamaIndex RRF→结构化降序结果；不修改业务数据库。"""
import asyncio
from contextlib import closing
from datetime import datetime, timezone, timedelta
import json
import math
from pathlib import Path
import sqlite3
import re

from llama_index.core.retrievers import BaseRetriever, QueryFusionRetriever
from llama_index.core.llms import MockLLM
from llama_index.core.schema import TextNode, NodeWithScore
from sqlite_vec import serialize_float32

from app.core.config import get_settings
from app.infrastructure.embedding import EmbeddingClient
from app.infrastructure.sqlite_fts import connect_memory_database
from app.agent.conversation_memory.prompt.embedding import MEMORY_QUERY_INSTRUCTION, MEMORY_EMBEDDING_MODEL
from app.schema.communication import ConversationHistoryRecord
from .schema import MemoryRetrievalResult, RankedTask


async def embed_query(text):
    settings = get_settings().embedding
    if settings.model != MEMORY_EMBEDDING_MODEL or settings.dimensions != 4096:
        raise ValueError('查询向量模型必须与记忆存储一致')
    prompt = f'<|im_start|>system\n{MEMORY_QUERY_INSTRUCTION}<|im_end|>\n<|im_start|>user\n{text}<|im_end|>\n<|im_start|>assistant\n'
    async with EmbeddingClient(settings) as client:
        result = await client.create_embeddings(inputs=[prompt])
    if result.model != settings.model or len(result.vectors) != 1:
        raise ValueError('查询向量响应模型或数量不一致')
    vector = result.vectors[0]
    if len(vector) != 4096 or any(isinstance(x, bool) or not math.isfinite(x) for x in vector):
        raise ValueError('查询向量无效')
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or norm == 0:
        raise ValueError('查询向量范数无效')
    return tuple(x / norm for x in vector)


def read_candidates(database, request, plan):
    clauses = ["r.conversation_id = ?", "r.kind = 'task'"]
    params = [request.conversation_id]
    for value, clause in [(plan.created_from, 'r.created_at >= ?'), (plan.created_before, 'r.created_at < ?')]:
        if value is not None:
            clauses.append(clause); params.append(value)
    if plan.statuses is not None:
        clauses.append('r.status IN (' + ','.join('?' for _ in plan.statuses) + ')')
        params.extend(plan.statuses)
    # 只读打开，避免错误路径创建空数据库；一条查询同时取得原任务与三字段投影。
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute('SELECT r.*, t.user_input_text, t.user_input_embedding, '
            't.intermediate_output_text, t.intermediate_output_embedding, t.final_output_text, t.final_output_embedding '
            'FROM conversation_records r JOIN conversation_task_retrievals t USING(record_id) WHERE '
            + ' AND '.join(clauses) + ' ORDER BY r.sequence, r.record_id', params).fetchall()
    return [dict(row) for row in rows]


class FieldRetriever(BaseRetriever):
    def __init__(self, rows, field_query, mode, top_k, encoder):
        super().__init__()
        self.rows = [r for r in rows if r[field_query.field + '_text'] is not None
                     and r[field_query.field + '_embedding'] is not None]
        self.field_query, self.mode, self.top_k, self.encoder = field_query, mode, top_k, encoder

    def _retrieve(self, query_bundle):
        raise RuntimeError('请使用异步aretrieve')

    async def _aretrieve(self, query_bundle):
        if not self.rows:
            return []
        vector = await self.encoder(self.field_query.query) if self.mode == 'vector' else None
        return await asyncio.to_thread(self.search, vector)

    def search(self, vector):
        field = self.field_query.field
        with closing(connect_memory_database(':memory:')) as connection:
            if self.mode == 'bm25':
                connection.execute("CREATE VIRTUAL TABLE docs USING fts5(content, tokenize='lindera_tokenizer')")
                connection.executemany('INSERT INTO docs(rowid,content) VALUES (?,?)',
                                       [(i+1, r[field+'_text']) for i,r in enumerate(self.rows)])
                connection.execute("CREATE VIRTUAL TABLE terms USING fts5(content, tokenize='lindera_tokenizer')")
                # 文件键是格式标签，不参与关键词召回；值仍保留原始线索。
                text = re.sub(r'(?m)^\s*(file_name|display_name|summary)\s*:', '', self.field_query.query)
                connection.execute('INSERT INTO terms VALUES (?)', (text,))
                connection.execute("CREATE VIRTUAL TABLE vocabulary USING fts5vocab(terms, 'row')")
                terms = [r[0] for r in connection.execute('SELECT term FROM vocabulary ORDER BY term')
                         if any(c.isalnum() for c in r[0])]
                if not terms:
                    return []
                match = ' OR '.join('"' + t.replace('"','""') + '"' for t in terms)
                hits = connection.execute('SELECT rowid, -bm25(docs) FROM docs WHERE docs MATCH ? '
                                          'ORDER BY bm25(docs),rowid LIMIT ?', (match,self.top_k)).fetchall()
            else:
                connection.execute('CREATE TABLE vectors(id INTEGER PRIMARY KEY, embedding BLOB)')
                connection.executemany('INSERT INTO vectors VALUES (?,?)',
                                       [(i+1,r[field+'_embedding']) for i,r in enumerate(self.rows)])
                hits = connection.execute('SELECT id, 1-vec_distance_cosine(embedding, ?) AS score '
                                          'FROM vectors ORDER BY score DESC,id LIMIT ?',
                                          (serialize_float32(vector),self.top_k)).fetchall()
        # 全路同一record_id生成同一内容hash；不把字段正文或路由分数写入节点身份。
        return [NodeWithScore(node=TextNode(text=self.rows[i-1]['record_id'],
                    id_=self.rows[i-1]['record_id']), score=score) for i,score in hits]


async def run_query(request, plan, *, database, encoder=embed_query, top_k=20):
    rows = await asyncio.to_thread(read_candidates, database, request, plan)
    if not rows:
        return MemoryRetrievalResult(status='success')
    retrievers = [FieldRetriever(rows, q, mode, top_k, encoder) for q in plan.queries for mode in ('bm25','vector')]
    # num_queries=1禁用扩写。显式无生成LLM避免框架自动解析默认OpenAI配置；该对象不会被调用。
    fusion = QueryFusionRetriever(retrievers, llm=MockLLM(), num_queries=1,
                                  mode='reciprocal_rerank', similarity_top_k=top_k, use_async=True)
    hits = await fusion.aretrieve(request.query)
    from .tasks import pull_ranked_tasks
    tasks = list(pull_ranked_tasks(rows, ((hit.node.node_id, hit.score) for hit in hits)))
    tasks.sort(key=lambda item: (-item.rrf_score, item.record.sequence, item.record.record_id))
    return MemoryRetrievalResult(status='success', tasks=tuple(tasks))
