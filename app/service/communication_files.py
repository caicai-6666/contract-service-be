"""归档附件校验与缺失恢复；只处理会话 upload 下已注册的 UUID 文件。"""

import hashlib
import os
from pathlib import Path
from uuid import UUID


def ensure_uploaded_files(directory: Path, records, retained: dict[str, bytes]) -> None:
    for record in records:
        for file in record.payload.get('input', {}).get('files', []):
            file_id = file.get('file_id')
            if not isinstance(file_id, str) or str(UUID(file_id)) != file_id:
                raise ValueError('附件标识不是规范 UUID')
            location = file.get('file_path')
            if location != f'/{file_id}.pdf':
                raise ValueError('附件路径不符合 upload 相对路径契约')
            path = directory / location[1:]
            if path.is_symlink():
                raise ValueError('附件不得为符号链接')
            content = retained.get(location)
            if not path.exists() and content is not None:
                directory.mkdir(parents=True, exist_ok=True)
                try:
                    handle = path.open('xb')
                except FileExistsError:
                    pass
                else:
                    try:
                        with handle:
                            handle.write(content)
                            handle.flush()
                            os.fsync(handle.fileno())
                    except BaseException:
                        path.unlink(missing_ok=True)
                        raise
            if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
                raise ValueError('已注册附件缺失或不可读取')
            # 新上传文件仍有原始字节时校验完整性；重启后的老文件不伪造缺失的哈希。
            with path.open('rb') as handle:
                digest = hashlib.file_digest(handle, 'sha256').digest()
            if content is not None and digest != hashlib.sha256(content).digest():
                raise ValueError('已注册附件与原始上传内容不一致')
