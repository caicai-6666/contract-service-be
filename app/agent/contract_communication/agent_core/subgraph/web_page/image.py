"""网页候选图片的提取和有界内存读取；不生成附件、不写磁盘。"""
import asyncio
import base64
import hashlib
import ipaddress
from io import BytesIO
from urllib.parse import urljoin, urldefrag
import warnings

import httpx
from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field
from app.infrastructure.public_dns import resolve_public_address as _public_address


class WebImage(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    number: int = Field(ge=1, strict=True, description="本次实际下载的候选图片编号，失败候选可形成编号间隙。")
    url: str = Field(description="图片重定向后实际读取的公开地址，仅用于来源定位。")
    caption: str = Field(default='', description='网页实际提供的图注或替代文字，不代表图片细节已核实。')
    context: str = Field(default='', description='图片附近的网页文字，用于判断对象归属。')
    width: int = Field(gt=0, description="标准化PNG的实际像素宽度。")
    height: int = Field(gt=0, description="标准化PNG的实际像素高度。")
    # 字节只驻留当前任务；结构化回执、审计和常规序列化均不包含图片内容。
    png: bytes = Field(exclude=True, repr=False, description="仅驻留当前任务的真实PNG字节，禁止进入工具回执或持久化。")

    def content_block(self):
        return {'type': 'image_url', 'image_url': {
            'url': 'data:image/png;base64,' + base64.b64encode(self.png).decode('ascii')}}


def _url(value, base=''):
    url = httpx.URL(urldefrag(urljoin(base, value))[0])
    if url.scheme not in ('http', 'https') or not url.host or url.userinfo or url.port not in (None, 80, 443):
        raise ValueError('图片地址不属于公开 HTTP 页面')
    return url


def extract_candidates(root, base_url, limit):
    """保留正文图像的图注和邻近文本；URL 去重，拒绝内嵌数据与导航装饰。"""
    roots = root.xpath('//article|//main') or [root]
    selected, seen = [], set()
    for content in roots:
        for element in content.xpath('.//img'):
            if any(parent.tag in ('nav', 'header', 'footer', 'aside', 'script', 'style') for parent in element.iterancestors()):
                continue
            marker = ' '.join(element.get(key, '') for key in ('class', 'id', 'role')).lower().split()
            if any(word in marker for word in ('logo', 'icon', 'avatar', 'ad', 'ads', 'advertisement', 'tracking', 'banner')):
                continue
            if any(element.get(key, '').isdigit() and int(element.get(key)) < 48 for key in ('width', 'height')):
                continue
            # 常见懒加载地址优先；srcset 选最后一个候选，实际大小仍由下载与解码边界限制。
            srcset = element.get('data-srcset') or element.get('srcset') or ''
            choices = [part.strip().split()[0] for part in srcset.split(',') if part.strip()]
            value = (element.get('data-src') or element.get('data-original') or
                     (choices[-1] if choices else '') or element.get('src', ''))
            try:
                url = str(_url(value, base_url)) if value else ''
            except (ValueError, httpx.InvalidURL):
                continue
            if not url or url in seen:
                continue
            seen.add(url)
            figure = next((parent for parent in element.iterancestors() if parent.tag == 'figure'), None)
            captions = figure.xpath('.//figcaption//text()') if figure is not None else []
            caption = ' '.join(' '.join(captions).split()) or ' '.join((element.get('alt') or element.get('title') or '').split())
            parent = figure if figure is not None else element.getparent()
            context = ' '.join(parent.text_content().split()) if parent is not None else ''
            selected.append({'url': url, 'caption': caption[:300], 'context': context[:700]})
            if len(selected) >= limit:
                return selected
    return selected


def _normalize(content, max_pixels, max_side, max_bytes):
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(BytesIO(content)) as source:
            if source.format not in ('JPEG', 'PNG', 'WEBP', 'GIF'):
                raise ValueError('不支持的网页图像类型')
            if source.width * source.height > max_pixels or min(source.size) < 48:
                raise ValueError('图像尺寸不适合读取')
            if getattr(source, 'n_frames', 1) != 1:
                raise ValueError('动态图片不能作为完整静态证据')
            image = ImageOps.exif_transpose(source).convert('RGBA')
            background = Image.new('RGBA', image.size, 'white')
            image = Image.alpha_composite(background, image).convert('RGB')
            image.thumbnail((max_side, max_side))
            output = BytesIO()
            image.save(output, format='PNG')
            png = output.getvalue()
            if len(png) > max_bytes:
                raise ValueError('解码后的图片超出读取上限')
            return png, image.size


async def download_images(candidates, *, transport=None, timeout_seconds=15, max_bytes=4*1024*1024,
                          max_pixels=16_000_000, max_side=1280, resolver=None):
    """逐图下载，共享总时限；固定经校验的公网 IP，逐跳校验，失败只影响该图。"""
    images, seen, failed = [], set(), 0
    resolver = resolver or _public_address
    addresses = {}
    try:
        async with asyncio.timeout(timeout_seconds):
            for index, candidate in enumerate(candidates, 1):
                try:
                    current = _url(candidate['url'])
                    for hop in range(4):
                        endpoint = (current.host, current.port or (443 if current.scheme == 'https' else 80))
                        # 仅在本次下载中复用已校验地址，三张同源图无需重复 HTTPS DNS 查询。
                        if endpoint not in addresses:
                            addresses[endpoint] = await resolver(*endpoint)
                        address = addresses[endpoint]
                        if not ipaddress.ip_address(address).is_global or ipaddress.ip_address(address).is_multicast:
                            raise ValueError('图片地址不是公网 IP')
                        target = current.copy_with(host=address)
                        # 连接使用已校验 IP；Host 与 TLS SNI 仍为原域名，避免二次 DNS 解析改变目标。
                        # 每一跳独立客户端，禁止 Cookie、代理或应用身份跨地址传播。
                        async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                                                     timeout=timeout_seconds) as client:
                            async with client.stream('GET', target, headers={'Host': current.netloc.decode(), 'Accept': 'image/*'},
                                                     extensions={'sni_hostname': current.host}) as response:
                                if response.is_redirect:
                                    if hop == 3 or 'location' not in response.headers:
                                        raise ValueError('图片重定向过多或无目标')
                                    current = _url(response.headers['location'], str(current))
                                    continue
                                response.raise_for_status()
                                media = response.headers.get('content-type', '').split(';')[0].strip().lower()
                                if media and media not in ('image/jpeg', 'image/png', 'image/webp', 'image/gif', 'application/octet-stream'):
                                    raise ValueError('响应不是受支持的图片')
                                data = bytearray()
                                async for chunk in response.aiter_bytes():
                                    if len(data) + len(chunk) > max_bytes:
                                        raise ValueError('图片下载超限')
                                    data.extend(chunk)
                        png, (width, height) = await asyncio.to_thread(_normalize, bytes(data), max_pixels, max_side, max_bytes)
                        digest = hashlib.sha256(png).digest()
                        if digest not in seen:
                            seen.add(digest)
                            images.append(WebImage(number=index, url=str(current), caption=candidate['caption'],
                                context=candidate['context'], width=width, height=height, png=png))
                        break
                except (ValueError, OSError, httpx.HTTPError, httpx.InvalidURL, Image.DecompressionBombError, Image.DecompressionBombWarning):
                    failed += 1
    except TimeoutError:
        failed += 1
    # 不向模型披露网络异常原文，也不把抓取失败当作“图片无关”。
    note = '部分候选图片未能读取或不适合静态查看，未提供的图片内容尚未核实。' if failed else ''
    return tuple(images), note
