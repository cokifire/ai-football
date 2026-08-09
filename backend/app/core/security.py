import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings


bearer_scheme = HTTPBearer(auto_error=False)


def _get_role(
    credentials: HTTPAuthorizationCredentials | None,
) -> str | None:
    if credentials is None or credentials.scheme.lower() != "bearer":
        return None

    token = credentials.credentials
    if settings.admin_api_key and secrets.compare_digest(token, settings.admin_api_key):
        return "admin"
    if settings.read_api_key and secrets.compare_digest(token, settings.read_api_key):
        return "read"
    return None


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="需要有效的 Bearer API key",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_read(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer_scheme)],
) -> str:
    role = _get_role(credentials)
    if role is None:
        raise _unauthorized()
    return role


def require_admin(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer_scheme)],
) -> str:
    if not settings.admin_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="管理 API 尚未配置认证密钥",
        )
    if _get_role(credentials) != "admin":
        raise _unauthorized()
    return "admin"


ReadAuth = Annotated[str, Depends(require_read)]
AdminAuth = Annotated[str, Depends(require_admin)]
