"""审核用户登录接口契约。"""

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class LoginRequest(BaseModel):
    """只携带审核用户密钥的登录请求。"""

    model_config = ConfigDict(extra="forbid")

    secret_key: SecretStr = Field(
        min_length=1,
        description="审核用户 YAML 中配置的非空密钥。",
    )


class LoginResponse(BaseModel):
    """登录成功后签发的免登码与审核人名称。"""

    login_code: str = Field(
        min_length=1,
        description="可在配置时效内解析为审核人名称的免登码。",
    )
    user_name: str = Field(
        min_length=1,
        description="当前密钥对应的审核人名称。",
    )


__all__ = ["LoginRequest", "LoginResponse"]
