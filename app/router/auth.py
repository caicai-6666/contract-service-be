"""查询统一登录后的当前用户；本平台不再提供凭据登录或令牌签发。"""
from fastapi import APIRouter, Response
from app.router.dependency import PlatformUserDependency
from app.schema.auth import AuthenticatedUser

router = APIRouter(prefix='/auth', tags=['auth'])


@router.get('/me', response_model=AuthenticatedUser, summary='查询当前登录用户及本平台访问资格',
    responses={401:{'description':'用户令牌缺失或登录失效'},403:{'description':'不属于本平台管理员'},
               503:{'description':'平台会话或中间件认证服务不可用'}})
async def current_user(user: PlatformUserDependency, response: Response) -> AuthenticatedUser:
    response.headers['Cache-Control'] = 'no-store'
    return user
