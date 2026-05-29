from functools import lru_cache
from typing import Any
from uuid import uuid4

import jwt
import requests  # type: ignore
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.config.settings import get_settings
from src.dependencies import get_service_container

bearer_scheme = HTTPBearer()
settings = get_settings()


def generate_access_token() -> Any:
    TOKEN_URL = settings.token_url

    data = {
        "grant_type": "client_credentials",
        "client_id": settings.client_id,
        "client_secret": settings.client_secret,
    }

    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    response = requests.post(TOKEN_URL, data=data, headers=headers)

    return response.json()


@lru_cache()
def get_jwks() -> Any:
    response = requests.get(settings.aws_cognito_jwks_url)
    return response.json()


def verify_cognito_token(token: str) -> None:
    try:
        jwks = get_jwks()
        header = jwt.get_unverified_header(token)
        key = next((k for k in jwks["keys"] if k["kid"] == header["kid"]), None)

        if not key:
            raise Exception()

        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(key)

        if not isinstance(public_key, RSAPublicKey):
            raise Exception("Expected RSA public key")

        jwt.decode(token, public_key, algorithms=["RS256"], issuer=settings.aws_cognito_issuer)  # type; ignore

    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


async def verify_token(credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)) -> None:

    if credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    verify_cognito_token(credentials.credentials)


async def get_session_id(session_id: str | None = Header(default=None, alias="X-Session-Id")) -> str:

    service_container = get_service_container()

    # Generate session ID if missing
    if not session_id or not session_id.strip():
        session_id = str(uuid4())

    session_id = session_id.strip()

    # Create session if not exists
    service_container.session_manager.get_or_create_session(session_id)

    return session_id
