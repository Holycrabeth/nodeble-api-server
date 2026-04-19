"""Bearer token authentication for API routes.

Public routes (only /health in M1.2) skip this dependency. Every other route
should `Depends(require_bearer_token)`.
"""
from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from nodeble_api_server.config import load_config

_bearer = HTTPBearer(auto_error=False)


async def require_bearer_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """Validate the Authorization: Bearer <token> header against api.yaml.

    Returns the matched token string on success. Raises 401 on any failure.
    Uses secrets.compare_digest for constant-time comparison (timing-safe).
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    valid_tokens = [entry.token for entry in load_config().tokens]
    provided = credentials.credentials
    for valid in valid_tokens:
        if secrets.compare_digest(provided, valid):
            return provided

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )
