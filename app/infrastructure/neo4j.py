"""Neo4j 异步连接与事务；不承载合同关系规则或图结构初始化。"""

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, Self, TypeVar

from neo4j import AsyncDriver, AsyncGraphDatabase, AsyncManagedTransaction, EagerResult

from app.core.config import Settings

T = TypeVar("T")


class Neo4jClient:
    """同一事件循环内共享连接池，每次操作创建独立会话。

    客户端拥有传入驱动的生命周期。关闭前由调用方等待在途操作结束，
    不能在执行查询时并发关闭客户端。
    """

    def __init__(self, settings: Settings, *, driver: AsyncDriver | None = None):
        self._database = settings.neo4j_database
        self._driver = driver if driver is not None else AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=None,
            connection_timeout=settings.neo4j_connection_timeout_seconds,
        )
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Neo4j 客户端已关闭")

    async def __aenter__(self) -> Self:
        self._ensure_open()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """释放连接池；重复关闭不产生额外操作。"""
        if not self._closed:
            await self._driver.close()
            self._closed = True

    async def verify_connectivity(self) -> None:
        """检查连接与目标数据库可读性；保留驱动原始异常。"""
        self._ensure_open()
        await self._driver.verify_connectivity()
        # 握手成功不代表配置的数据库存在，因此额外执行目标库查询。
        await self.query("RETURN 1 AS ok")

    async def query(
        self,
        cypher: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        mode: Literal["read", "write"] = "read",
    ) -> EagerResult:
        """执行单条参数化查询，返回已消费的 records、summary 和 keys。

        结果全部载入内存，查询调用方负责限制返回规模。读模式仅表达
        路由意图，不是权限隔离；写入语句必须显式使用 write。
        """
        if mode not in ("read", "write"):
            raise ValueError("Neo4j 查询模式必须为 read 或 write")

        async def run(tx: AsyncManagedTransaction) -> EagerResult:
            result = await tx.run(cypher, parameters=dict(parameters or {}))
            # 禁止把绑定事务的懒结果游标带出已经关闭的会话。
            return await result.to_eager_result()

        if mode == "read":
            return await self.execute_read(run)
        return await self.execute_write(run)

    async def execute_read(
        self, work: Callable[[AsyncManagedTransaction], Awaitable[T]],
    ) -> T:
        """执行读事务；回调须在事务内部消费结果，返回普通值或已物化结果。"""
        self._ensure_open()
        async with self._driver.session(
            database=self._database,
            bookmark_manager=self._driver.execute_query_bookmark_manager,
        ) as session:
            return await session.execute_read(work)

    async def execute_write(
        self, work: Callable[[AsyncManagedTransaction], Awaitable[T]],
    ) -> T:
        """执行原子写事务；异常回滚，瞬时故障由官方驱动有限重试。

        回调可能重复执行，不可在内部调用模型、写文件或更新 SQLite；
        跨存储副作用应由业务层协调，不属于 Neo4j 事务的原子范围。
        """
        self._ensure_open()
        async with self._driver.session(
            database=self._database,
            bookmark_manager=self._driver.execute_query_bookmark_manager,
        ) as session:
            return await session.execute_write(work)
