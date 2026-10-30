"""公开图片地址解析：兼容 Fake-IP DNS，但连接目标始终为已校验公网地址。"""
import asyncio
import ipaddress
import json
import socket

import httpx

_FAKE_IP_NETWORK = ipaddress.ip_network('198.18.0.0/15')
_DOH_URL = 'https://1.1.1.1/dns-query'


def _public_addresses(values):
    addresses = tuple(dict.fromkeys(ipaddress.ip_address(value) for value in values))
    if not addresses or any(not address.is_global or address.is_multicast for address in addresses):
        raise ValueError('图片地址不能指向本地或内网')
    return tuple(str(address) for address in addresses)


async def _resolve_doh(host, *, transport=None):
    # 固定 HTTPS 公网解析器，避免解析器本身再次经过本地 Fake-IP DNS；不跟随重定向或使用代理。
    name = host.rstrip('.').encode('idna').decode('ascii').lower()
    async with asyncio.timeout(5):
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5, transport=transport) as client:
            async with client.stream('GET', _DOH_URL, params={'name': name, 'type': 'A'},
                                     headers={'Accept': 'application/dns-json'}) as response:
                if response.status_code != 200:
                    raise ValueError('公网 DNS 解析不可用')
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > 65536:
                        raise ValueError('公网 DNS 响应超限')
                    content.extend(chunk)
    payload = json.loads(content)
    if (not isinstance(payload, dict) or type(payload.get('Status')) is not int or payload['Status'] != 0
            or payload.get('TC') is not False):
        raise ValueError('公网 DNS 响应不完整')
    questions = payload.get('Question')
    if (not isinstance(questions, list) or len(questions) != 1 or not isinstance(questions[0], dict)
            or type(questions[0].get('type')) is not int or questions[0]['type'] != 1
            or not isinstance(questions[0].get('name'), str)
            or questions[0].get('name', '').rstrip('.').lower() != name):
        raise ValueError('公网 DNS 响应与查询不一致')
    answers = payload.get('Answer')
    if not isinstance(answers, list) or any(not isinstance(row, dict) for row in answers):
        raise ValueError('公网 DNS 未返回地址')
    records = [row for row in answers if row.get('type') in (1, 28)]
    addresses = []
    for row in records:
        if type(row.get('type')) is not int or not isinstance(row.get('data'), str):
            raise ValueError('公网 DNS 地址不合法')
        address = ipaddress.ip_address(row['data'])
        if address.version != (4 if row['type'] == 1 else 6):
            raise ValueError('公网 DNS 地址类型不一致')
        addresses.append(str(address))
    return _public_addresses(addresses)[0]


async def resolve_public_address(host, port, *, doh_transport=None):
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        records = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = tuple(dict.fromkeys(ipaddress.ip_address(row[4][0]) for row in records))
    else:
        # URL 直接提供非公网 IP 时始终拒绝，不能借助 Fake-IP 兼容路径放行。
        return _public_addresses([str(literal)])[0]
    if addresses and all(address.version == 4 and address in _FAKE_IP_NETWORK for address in addresses):
        # 仅全为已知 Fake-IP 网段时补做独立解析；混入内网/回环等地址仍按原规则拒绝。
        return await _resolve_doh(host, transport=doh_transport)
    return _public_addresses([str(address) for address in addresses])[0]
