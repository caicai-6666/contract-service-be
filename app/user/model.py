"""审核用户及其启动期内存快照对象。"""

from __future__ import annotations

from hashlib import sha256
from hmac import compare_digest
from pathlib import Path
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

ReviewerName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
]


class ReviewerUserModel(BaseModel):
    """审核用户配置使用的不可变严格基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ReviewerUser(ReviewerUserModel):
    """一个审核人名称及其对应密钥。"""

    name: ReviewerName
    # SecretStr 避免对象被日志或异常直接打印时泄漏密钥明文。
    secret_key: SecretStr = Field(min_length=1)

    @field_validator("secret_key")
    @classmethod
    def validate_secret_key(cls, value: SecretStr) -> SecretStr:
        """拒绝仅由空白字符组成的密钥。"""
        if not value.get_secret_value().strip():
            raise ValueError("审核用户密钥不能为空")
        return value

    def matches_secret_key(self, candidate: str) -> bool:
        """使用常量时间比较检查候选密钥。"""
        expected = self.secret_key.get_secret_value().encode("utf-8")
        actual = candidate.encode("utf-8")
        return compare_digest(expected, actual)


class ReviewerUserFile(ReviewerUserModel):
    """`users.yaml` 的严格文件结构。"""

    users: tuple[ReviewerUser, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_names(self) -> Self:
        """同一文件中的审核人名称与密钥必须分别唯一。"""
        names = [user.name for user in self.users]
        if len(names) != len(set(names)):
            raise ValueError("审核人名称不能重复")
        secret_keys = [
            user.secret_key.get_secret_value() for user in self.users
        ]
        if len(secret_keys) != len(set(secret_keys)):
            raise ValueError("审核用户密钥不能重复")
        return self


class ReviewerUserCatalog(ReviewerUserModel):
    """应用启动时加载的审核用户不可变内存快照。"""

    source_path: Path
    users: tuple[ReviewerUser, ...] = Field(min_length=1)
    content_sha256: Annotated[
        str,
        StringConstraints(pattern=r"^[0-9a-f]{64}$"),
    ]

    @model_validator(mode="after")
    def validate_unique_names(self) -> Self:
        """内存快照中的审核人名称与密钥必须分别唯一。"""
        names = [user.name for user in self.users]
        if len(names) != len(set(names)):
            raise ValueError("审核人名称不能重复")
        secret_keys = [
            user.secret_key.get_secret_value() for user in self.users
        ]
        if len(secret_keys) != len(set(secret_keys)):
            raise ValueError("审核用户密钥不能重复")
        return self

    def get(self, name: str) -> ReviewerUser:
        """按审核人名称返回用户对象，不存在时明确失败。"""
        for user in self.users:
            if user.name == name:
                return user
        raise KeyError(f"未知审核用户：{name}")

    def authenticate(self, name: str, secret_key: str) -> ReviewerUser | None:
        """校验名称和密钥，成功时返回对应用户对象。"""
        try:
            user = self.get(name)
        except KeyError:
            return None
        return user if user.matches_secret_key(secret_key) else None

    def find_by_secret_key(self, secret_key: str) -> ReviewerUser | None:
        """仅凭密钥查找审核用户；始终扫描完整目录。"""
        matched_user: ReviewerUser | None = None
        for user in self.users:
            if user.matches_secret_key(secret_key):
                matched_user = user
        return matched_user

    @classmethod
    def from_file(
        cls,
        source_path: Path,
        definition: ReviewerUserFile,
        raw_content: bytes,
    ) -> ReviewerUserCatalog:
        """根据已校验文件构造带内容指纹的快照。"""
        return cls(
            source_path=source_path,
            users=definition.users,
            content_sha256=sha256(raw_content).hexdigest(),
        )


__all__ = [
    "ReviewerUser",
    "ReviewerUserCatalog",
    "ReviewerUserFile",
]
