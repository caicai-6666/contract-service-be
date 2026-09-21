"""合同图节点与不可修改的无向关联维护。"""

import re
import math
from uuid import UUID
from dataclasses import dataclass
from datetime import datetime

from neo4j import AsyncManagedTransaction

from app.infrastructure.neo4j import Neo4jClient


class ContractRelationExistsError(RuntimeError):
    """相同无向端点之间已有关联。"""


class ContractGraphNodeMissingError(RuntimeError):
    """正式合同的图节点缺失，需同步后重试。"""


@dataclass(frozen=True, slots=True)
class ContractRelation:
    relation_id: str
    document_id_a: str
    document_id_b: str
    description: str
    created_at: datetime
    created_by: str


@dataclass(frozen=True, slots=True)
class ContractNeighbor:
    relation_id: str
    document_id: str
    description: str
    created_at: datetime
    created_by: str


class ContractGraphStore:
    """图中保存合同身份和关系属性；合同元数据仍以 SQLite 为准。"""

    def __init__(self, client: Neo4jClient):
        self._client = client

    async def initialize(self) -> None:
        await self._client.verify_connectivity()
        await self._client.query(
            "CREATE CONSTRAINT contract_document_id_unique IF NOT EXISTS "
            "FOR (n:Contract) REQUIRE n.document_id IS UNIQUE",
            mode="write",
        )

        # 内部规范化端点键用于数据库级去重，不作为用户可编辑属性。
        await self._client.query(
            "CREATE CONSTRAINT contract_relation_pair_unique IF NOT EXISTS "
            "FOR ()-[r:RELATED_TO]-() REQUIRE r.pair_key IS UNIQUE", mode="write",
        )
        await self._client.query(
            "CREATE CONSTRAINT contract_relation_id_unique IF NOT EXISTS "
            "FOR ()-[r:RELATED_TO]-() REQUIRE r.relation_id IS UNIQUE", mode="write",
        )

    @staticmethod
    def _validate_id(document_id: str) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", document_id) is None:
            raise ValueError("document_id 必须是 64 位小写 SHA-256")

    async def ensure_contract(self, document_id: str) -> None:
        self._validate_id(document_id)
        await self._client.query(
            "MERGE (:Contract {document_id: $document_id})",
            {"document_id": document_id}, mode="write",
        )

    async def delete_contract(self, document_id: str) -> None:
        self._validate_id(document_id)
        # 单个事务清除节点及全部入边、出边；不存在视为已完成。
        await self._client.query(
            "MATCH (n:Contract {document_id: $document_id}) DETACH DELETE n",
            {"document_id": document_id}, mode="write",
        )


    async def create_relation(
        self, relation: ContractRelation, *, description_embedding: tuple[float, ...],
        embedding_model: str, embedding_version: str, embedding_dimensions: int,
    ) -> ContractRelation:
        """属性仅在首次创建时写入，已有关系返回冲突，不修改描述或审计字段。"""
        a, b = sorted((relation.document_id_a, relation.document_id_b))
        self._validate_id(a)
        self._validate_id(b)
        if a == b:
            raise ValueError("不能将合同关联到自身")
        if not relation.description.strip() or len(relation.description) > 10000:
            raise ValueError("关系描述必须为 1 至 10000 字符")
        if not relation.created_by.strip() or relation.created_at.utcoffset() is None:
            raise ValueError("关系创建人不能为空且时间必须包含时区")
        # 基础设施独立守住存储边界，不依赖服务层已做过校验。
        if (embedding_dimensions <= 0 or len(description_embedding) != embedding_dimensions
                or not embedding_model.strip() or not embedding_version.strip()
                or any(isinstance(x, bool) or not isinstance(x, (int, float))
                       or not math.isfinite(x) for x in description_embedding)):
            raise ValueError("关系描述向量或编码信息无效")
        norm = math.hypot(*description_embedding)
        if not math.isfinite(norm) or norm == 0:
            raise ValueError("关系描述向量范数无效")

        async def create(tx: AsyncManagedTransaction) -> ContractRelation:
            result = await tx.run(
                "MATCH (a:Contract {document_id:$a}), (b:Contract {document_id:$b}) "
                "MERGE (a)-[r:RELATED_TO]-(b) "
                "ON CREATE SET r.pair_key=$pair_key, r.relation_id=$relation_id, "
                "r.description=$description, r.created_at=$created_at, r.created_by=$created_by, "
                "r.description_embedding=$description_embedding, "
                "r.embedding_model=$embedding_model, r.embedding_version=$embedding_version, "
                "r.embedding_dimensions=$embedding_dimensions "
                "RETURN r.relation_id AS relation_id",
                a=a, b=b, pair_key=a + ":" + b, relation_id=relation.relation_id,
                description=relation.description, created_at=relation.created_at, created_by=relation.created_by,
                description_embedding=list(description_embedding), embedding_model=embedding_model,
                embedding_version=embedding_version, embedding_dimensions=embedding_dimensions,
            )
            record = await result.single()
            if record is None:
                raise ContractGraphNodeMissingError("合同图节点缺失，请同步后重试")
            # 驱动重试复用同一个 UUID；已提交但响应丢失时可安全识别本次创建。
            if record["relation_id"] != relation.relation_id:
                raise ContractRelationExistsError("两份合同之间已存在关联；关系不可修改，请删除后重建")
            return ContractRelation(relation.relation_id, a, b, relation.description,
                                    relation.created_at, relation.created_by)

        return await self._client.execute_write(create)


    async def delete_relation(self, relation_id: str) -> bool:
        """按不可复用的关系 UUID 删除单条边，保留两端合同及其他关系。"""
        normalized_id = str(UUID(relation_id))
        result = await self._client.query(
            "MATCH (:Contract)-[r:RELATED_TO {relation_id:$relation_id}]->(:Contract) DELETE r",
            {"relation_id": normalized_id}, mode="write",
        )
        # 以提交结果判定是否实际删除，避免先查后删的竞态窗口。
        return result.summary.counters.relationships_deleted > 0


    async def statistics(self, selected_ids: list[str], ready_ids: list[str]) -> dict:
        """只读精确聚合，无 Top K 截断；两端均须在 SQLite 正式目录内。"""
        if not selected_ids:
            return {'total': 0, 'contracts_with_relations': 0, 'contracts_without_relations': 0}
        rows = await self._client.query(
            """MATCH (a:Contract)-[r:RELATED_TO]->(b:Contract)
            WHERE a.document_id IN $ready_ids AND b.document_id IN $ready_ids
              AND (a.document_id IN $selected_ids OR b.document_id IN $selected_ids)
            WITH count(DISTINCT r.relation_id) AS total,
                 collect(DISTINCT a.document_id) + collect(DISTINCT b.document_id) AS endpoints
            RETURN total, size([id IN $selected_ids WHERE id IN endpoints]) AS linked""",
            {'selected_ids': selected_ids, 'ready_ids': ready_ids}, mode='read')
        row = rows.records[0]
        return {'total': row['total'], 'contracts_with_relations': row['linked'],
                'contracts_without_relations': len(selected_ids) - row['linked']}

    async def list_relations(self, document_id: str) -> list[ContractNeighbor]:
        """忽略边方向，只返回一跳邻居；缺失节点与无关联分开处理。"""
        self._validate_id(document_id)
        result = await self._client.query(
            "MATCH (n:Contract {document_id:$document_id}) "
            "OPTIONAL MATCH (n)-[r:RELATED_TO]-(other:Contract) "
            "RETURN other.document_id AS document_id, r.relation_id AS relation_id, "
            "r.description AS description, r.created_at AS created_at, r.created_by AS created_by "
            "ORDER BY r.created_at DESC, r.relation_id ASC",
            {"document_id": document_id},
        )
        if not result.records:
            raise ContractGraphNodeMissingError("合同图节点缺失，请同步后重试")
        neighbors = []
        for record in result.records:
            if record["document_id"] is None:
                continue
            # 官方驱动的 DateTime 转成带时区的 Python datetime，供 HTTP 序列化。
            stamp = record["created_at"]
            neighbors.append(ContractNeighbor(
                relation_id=record["relation_id"], document_id=record["document_id"],
                description=record["description"], created_at=stamp.to_native(),
                created_by=record["created_by"],
            ))
        return neighbors


    async def search_candidates(self, contract_id=None, *, limit=10000, relation_ids=None):
        """有起点时先匹配邻边；无起点按物理方向读取一次，避免无向边重复。"""
        if contract_id is not None:
            self._validate_id(contract_id)
            if not (await self._client.query('MATCH (n:Contract {document_id:$id}) RETURN n.document_id AS id', {'id':contract_id})).records:
                raise ContractGraphNodeMissingError('合同图节点缺失，请同步后重试')
        pattern = ('MATCH (a:Contract {document_id:$id})-[r:RELATED_TO]-(b:Contract) '
                   if contract_id is not None else 'MATCH (a:Contract)-[r:RELATED_TO]->(b:Contract) ')
        where = 'WHERE r.relation_id IN $ids ' if relation_ids is not None else ''
        result = await self._client.query(pattern+where+
            'RETURN a.document_id AS document_id_a,b.document_id AS document_id_b, '
            'r.relation_id AS relation_id,r.description AS description, '
            'r.description_embedding AS description_embedding,r.embedding_model AS embedding_model, '
            'r.embedding_version AS embedding_version,r.embedding_dimensions AS embedding_dimensions '
            'ORDER BY r.relation_id LIMIT $limit',
            {'id':contract_id,'ids':list(relation_ids) if relation_ids is not None else None,'limit':limit+1})
        if len(result.records)>limit:
            raise ValueError('关系候选超过安全上限，请指定合同缩小范围')
        return [dict(r) for r in result.records]
