"""Fernet crypto for stored third-party credentials.

sites.credentials_encrypted and connections.credentials_encrypted hold one
Fernet-encrypted JSON object each; the shape is per platform (documented on
the adapter that consumes it). The key is TOKEN_ENCRYPTION_KEY — a fixed dev
key locally, required from the environment when deployed."""

import json

from cryptography.fernet import Fernet

import settings


def encrypt_credentials(credentials: dict) -> str:
    return Fernet(settings.TOKEN_ENCRYPTION_KEY.encode()).encrypt(json.dumps(credentials).encode()).decode()


def decrypt_credentials(credentials_encrypted: str) -> dict:
    return json.loads(Fernet(settings.TOKEN_ENCRYPTION_KEY.encode()).decrypt(credentials_encrypted.encode()))
