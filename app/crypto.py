from cryptography.fernet import Fernet
from app import config

if not config.FERNET_KEY:
    raise RuntimeError(
        'FERNET_KEY is not set. Generate one with:\n'
        '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
    )

_f = Fernet(config.FERNET_KEY.encode())


def encrypt(plain: str) -> str:
    return _f.encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    if not token:
        return ""
    return _f.decrypt(token.encode()).decode()
