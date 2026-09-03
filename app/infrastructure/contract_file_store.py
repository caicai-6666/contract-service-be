"""按受限文件地址访问本地正式合同 PDF。"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from app.infrastructure.pdf_candidate_loader import DEFAULT_CONTRACT_FILE_ROOT


class InvalidContractFileAddressError(ValueError):
    """合同文件地址不符合本地文件存储协议。"""


class ContractFileNotFoundError(FileNotFoundError):
    """文件地址有效，但对应合同 PDF 不存在。"""


class LocalContractFileStore:
    """把根相对 `file_uri` 安全解析为 `data/contract` 下的 PDF。"""

    def __init__(self, *, root: Path = DEFAULT_CONTRACT_FILE_ROOT) -> None:
        resolved_root = root.resolve()
        if not resolved_root.is_dir():
            raise ValueError(f"合同 PDF 根目录不存在：{resolved_root}")
        self._root = resolved_root

    @property
    def root(self) -> Path:
        """返回固定的合同 PDF 根目录。"""
        return self._root

    def resolve(self, file_uri: str) -> Path:
        """校验文件地址，并返回根目录内实际存在的 PDF 路径。"""
        parsed = urlsplit(file_uri)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            raise InvalidContractFileAddressError(
                "file_uri 不能包含协议、主机、查询参数或片段"
            )

        decoded_path = unquote(parsed.path)
        uri_path = PurePosixPath(decoded_path)
        if (
            not decoded_path.startswith("/")
            or uri_path.parent != PurePosixPath("/")
            or not uri_path.name
            or uri_path.suffix.lower() != ".pdf"
        ):
            raise InvalidContractFileAddressError(
                "file_uri 必须是根相对的单层 PDF 地址"
            )

        resolved_path = (self._root / uri_path.name).resolve()
        # resolve 会展开符号链接；展开后不再直属根目录即视为目录逃逸。
        if resolved_path.parent != self._root:
            raise InvalidContractFileAddressError(
                "file_uri 解析结果超出合同 PDF 根目录"
            )
        if not resolved_path.is_file():
            raise ContractFileNotFoundError(uri_path.name)
        return resolved_path


__all__ = [
    "ContractFileNotFoundError",
    "InvalidContractFileAddressError",
    "LocalContractFileStore",
]
