"""Password hashing."""
import hashlib
import hmac
import os


def hash_password(password):
    salt = os.urandom(16).hex()
    value = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, maxmem=64*1024*1024)
    return f'scrypt${salt}${value.hex()}'


def verify_password(password, encoded):
    if not isinstance(password, str):
        return False
    try:
        algorithm, salt, expected = encoded.split('$')
        if algorithm != 'scrypt':
            return False
        value = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, maxmem=64*1024*1024)
        return hmac.compare_digest(value.hex(), expected)
    except (ValueError, TypeError, AttributeError):
        return False
