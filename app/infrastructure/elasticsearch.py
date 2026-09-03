"""Elasticsearch 异步客户端的创建与注入。"""

from collections.abc import AsyncIterator
from typing import cast

from elasticsearch import AsyncElasticsearch
from fastapi import Request

from app.core.config import Settings


def create_elasticsearch_client(settings: Settings) -> AsyncElasticsearch:
    """为无认证 HTTP 节点创建供启动探测与运行期复用的共享客户端。"""
    return AsyncElasticsearch(settings.elasticsearch_hosts)


async def get_elasticsearch_client(request: Request) -> AsyncIterator[AsyncElasticsearch]:
    """为需要持久化或检索的接口提供应用级 ES 客户端。"""
    yield cast(AsyncElasticsearch, request.app.state.elasticsearch)
