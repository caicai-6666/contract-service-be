"""固定源码构建含Jieba词典的SQLite扩展；不修改业务数据库。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
COMMIT = '6aed060cc6af06edf36de983ad8eb8fc6e34bbd4'
SOURCE_SHA256 = '905e40df15c9a7ab4542f97e28675ed10b9027465fcd7d14a1e82a0e1b29b31a'
URL = f'https://codeload.github.com/lindera/lindera-sqlite/tar.gz/{COMMIT}'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--direct', action='store_true', help='下载和构建不使用代理环境变量。')
    args = parser.parse_args()
    cargo = shutil.which('cargo') or str(Path.home() / '.cargo/bin/cargo')
    if not Path(cargo).is_file():
        raise SystemExit('请先安装Rust/Cargo工具链：https://rustup.rs/')
    suffix = {'Darwin': '.dylib', 'Linux': '.so'}.get(platform.system())
    if suffix is None:
        raise SystemExit('本安装脚本目前支持macOS和Linux。')
    work = ROOT / 'output/lindera-install'
    work.mkdir(parents=True, exist_ok=True)
    archive = work / 'source.tar.gz'
    if not archive.exists():
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if args.direct else urllib.request.build_opener()
        with opener.open(URL, timeout=120) as response:
            archive.write_bytes(response.read())
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise SystemExit('源码SHA256不匹配，请检查output/lindera-install/source.tar.gz。')
    source = work / f'lindera-sqlite-{COMMIT}'
    if not source.exists():
        with tarfile.open(archive) as bundle:
            bundle.extractall(work, filter='data')
    # 上游未提交Cargo.lock，使用项目保留的解析结果固定所有传递依赖。
    shutil.copy2(ROOT / 'config/lindera-Cargo.lock', source / 'Cargo.lock')
    env = os.environ.copy()
    if args.direct:
        for key in tuple(env):
            if key.lower() in {'http_proxy', 'https_proxy', 'all_proxy'}:
                del env[key]
    env['PATH'] = str(Path(cargo).parent) + os.pathsep + env.get('PATH', '')
    # 锁定上游依赖；只内嵌中文Jieba，不编入日文和韩文词典。
    subprocess.run([cargo, 'build', '--locked', '--release', '--features', 'embed-jieba'], cwd=source, env=env, check=True)
    library = source / 'target/release' / ('liblindera_sqlite' + suffix)
    destination = ROOT / 'data/extensions/lindera'
    destination.mkdir(parents=True, exist_ok=True)
    temporary = destination / ('liblindera_sqlite' + suffix + '.tmp')
    shutil.copy2(library, temporary)
    installed = destination / library.name
    temporary.replace(installed)
    shutil.copy2(source / 'LICENSE', destination / 'LICENSE')
    manifest = {'commit': COMMIT, 'source_url': URL, 'source_sha256': SOURCE_SHA256,
                'feature': 'embed-jieba', 'platform': platform.platform(),
                'library_sha256': hashlib.sha256(installed.read_bytes()).hexdigest()}
    (destination / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    print(f'已安装：{installed}')


if __name__ == '__main__':
    main()
