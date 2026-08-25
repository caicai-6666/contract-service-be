"""Elasticsearch 异步客户端的创建与注入。"""

from collections.abc import AsyncIterator
from typing import Any, cast

from elasticsearch import AsyncElasticsearch
from fastapi import Request

from app.core.config import Settings


def create_elasticsearch_client(settings: Settings) -> AsyncElasticsearch:
    """按当前配置创建共享客户端，不在启动阶段执行网络探测。"""
    options: dict[str, Any] = {
        "ca_certs": str(settings.elasticsearch_ca_cert_path),
        "verify_certs": settings.elasticsearch_verify_certs,
    }
    if settings.elasticsearch_username and settings.elasticsearch_password:
        options["basic_auth"] = (
            settings.elasticsearch_username,
            settings.elasticsearch_password,
        )
    return AsyncElasticsearch(settings.elasticsearch_hosts, **options)


async def get_elasticsearch_client(request: Request) -> AsyncIterator[AsyncElasticsearch]:
    """为需要持久化或检索的接口提供应用级 ES 客户端。"""
    yield cast(AsyncElasticsearch, request.app.state.elasticsearch)
