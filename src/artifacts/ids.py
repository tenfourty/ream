import secrets

_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"  # base32, lowercase, DNS-safe


def new_id() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(26))
