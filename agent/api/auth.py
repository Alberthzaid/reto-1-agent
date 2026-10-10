"""Authentication dependency for the shared team bearer token."""

import hmac
from collections.abc import Callable
from typing import Annotated

from fastapi import Header, HTTPException, status


def team_auth(team_token: str | None) -> Callable[..., None]:
    """Build an authorization dependency using constant-time token comparison."""

    def require_team_token(authorization: Annotated[str | None, Header()] = None) -> None:
        prefix = "Bearer "
        token = authorization.removeprefix(prefix) if authorization else ""
        if (
            not team_token
            or not authorization
            or not authorization.startswith(prefix)
            or not hmac.compare_digest(token, team_token)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="No autorizado",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return require_team_token