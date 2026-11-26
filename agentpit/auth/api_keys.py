import hashlib
import secrets


def new_api_key() -> str:
    """A fresh agent key: 256 random bits, URL-safe and dot-free, so the MCP
    verifier never mistakes it for a JWT."""
    return secrets.token_urlsafe(32)


def key_hash(key: str) -> str:
    """What the database keeps of a regenerated key. A plain sha256 is enough
    for a random 256-bit secret; nothing about it is guessable to stretch."""
    return hashlib.sha256(key.encode()).hexdigest()
