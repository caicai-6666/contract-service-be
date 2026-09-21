"""将合同签订日期迁移为 date；保留旧索引，校验通过后由操作者切换配置。"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from elasticsearch.helpers import async_scan
from app.core.config import get_settings
from app.infrastructure.elasticsearch import create_elasticsearch_client


def date_mapping(mapping):
    """保留源索引全部字段，仅修改日期类型，避免丢失历史配置字段。"""
    result = deepcopy(mapping)
    field = result['properties']['core']['properties']['signing_date']
    if field.get('type') != 'keyword':
        raise ValueError('仅接受 signing_date 为 keyword 的源索引')
    result['properties']['core']['properties']['signing_date'] = {
        'type': 'date', 'format': 'strict_date',
    }
    return result


async def fingerprints(client, index):
    """逐文档对照 ID 与可读取原文；仅保留摘要，避免复制全文到日志。"""
    values = {}
    async for hit in async_scan(client, index=index, query={'query': {'match_all': {}}}):
        raw = json.dumps(hit['_source'], sort_keys=True, ensure_ascii=False, allow_nan=False)
        values[hit['_id']] = hashlib.sha256(raw.encode()).hexdigest()
    return values


async def migrate(client, source, target):
    if source == target or any(c in source + target for c in '*?,'):
        raise ValueError('必须指定不同的精确源、目标索引名称')
    mappings = await client.indices.get_mapping(index=source)
    if set(mappings) != {source}:
        raise ValueError('源必须是单个实体索引，不能使用别名')
    mapping = date_mapping(mappings[source]['mappings'])
    if await client.indices.exists(index=target):
        raise ValueError('目标索引已存在，拒绝覆盖')
    original = (await client.indices.get_settings(index=source))[source]['settings']['index']
    was_blocked = str(original.get('blocks', {}).get('write', 'false')).lower() == 'true'
    # 先等待写屏障生效，再读取快照；成功后保留写屏障，防止切换前继续写旧库。
    await client.indices.add_block(index=source, block='write')
    succeeded = False
    try:
        await client.indices.refresh(index=source)
        before = await fingerprints(client, source)
        settings = {k: original[k] for k in (
            'number_of_shards', 'number_of_replicas', 'analysis', 'similarity'
        ) if k in original}
        created = await client.indices.create(index=target, settings=settings, mappings=mapping)
        if not created.get('acknowledged'):
            raise RuntimeError('目标索引创建未确认')
        response = await client.options(request_timeout=300).reindex(
            source={'index': source}, dest={'index': target, 'op_type': 'create'},
            refresh=True, wait_for_completion=True,
        )
        if response.get('failures') or response.get('timed_out') or response.get('created') != len(before):
            raise RuntimeError('迁移不完整，目标不可切换；旧索引仍保留')
        if before != await fingerprints(client, target):
            raise RuntimeError('迁移后文档 ID 或内容不一致，目标不可切换')
        # 实际执行日期范围与排序，验证新索引具备日期查询能力。
        await client.search(index=target, size=1, query={
            'range': {'core.signing_date': {'gte': '0001-01-01', 'lte': '9999-12-31'}}
        }, sort=[{'core.signing_date': 'asc'}])
        succeeded = True
        return {'source': source, 'target': target, 'documents': len(before),
                'verified': True, 'source_write_blocked': True}
    finally:
        if not succeeded and not was_blocked:
            # 失败目标保留供排查，但绝不替换源，也不修改业务配置。
            await client.indices.put_settings(index=source, settings={'index.blocks.write': False})


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--target', required=True)
    args = parser.parse_args()
    async with create_elasticsearch_client(get_settings()) as client:
        print(json.dumps(await migrate(client, args.source, args.target), ensure_ascii=False))
    print('校验完成。将 ELASTICSEARCH_INDEX_NAME 改为目标索引并重启后端；旧索引保留为只读备份。')


if __name__ == '__main__':
    asyncio.run(main())
