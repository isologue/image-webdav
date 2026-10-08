import hashlib
import json
import os
import secrets
import tempfile
import threading
from pathlib import Path

from passlib.hash import apr_md5_crypt, pbkdf2_sha256
from wsgidav.dc.base_dc import BaseDomainController


CONFIG = Path('/config')
SETTINGS = CONFIG / 'settings.json'
_lock = threading.RLock()


def atomic_write(path, contents):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.tmp-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            output.write(contents)
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def settings():
    with _lock:
        return json.loads(SETTINGS.read_text(encoding='utf-8'))


def update_settings(username, password, public_base):
    with _lock:
        current = settings()
        current.update(username=username, public_base=public_base)
        if password:
            current['password_hash'] = pbkdf2_sha256.hash(password)
        atomic_write(SETTINGS, json.dumps(current))


def prepare():
    CONFIG.mkdir(parents=True, exist_ok=True)
    with _lock:
        if SETTINGS.exists():
            current = settings()
            if 'password_hash' not in current:
                # Import credentials from earlier deployments without resetting them.
                legacy = CONFIG / 'dav.htpasswd'
                username, hashed = legacy.read_text(encoding='utf-8').strip().split(':', 1)
                if username != current['username']:
                    raise RuntimeError('Stored credentials do not match the configured username')
                current['password_hash'] = hashed
                atomic_write(SETTINGS, json.dumps(current))
        else:
            atomic_write(SETTINGS, json.dumps({
                'username': os.environ.get('INITIAL_USERNAME', 'admin'),
                'password_hash': pbkdf2_sha256.hash(os.environ.get('INITIAL_PASSWORD', 'admin2026')),
                'public_base': '',
            }))
        secret = CONFIG / 'session.key'
        if not secret.exists():
            atomic_write(secret, secrets.token_urlsafe(48))
        return secret.read_text(encoding='utf-8')


def verify_credentials(username, password):
    current = settings()
    if not secrets.compare_digest(username.encode(), current['username'].encode()):
        return False
    hashed = current['password_hash']
    verifier = apr_md5_crypt if hashed.startswith('$apr1$') else pbkdf2_sha256
    return verifier.verify(password, hashed)


def session_version():
    current = settings()
    return hashlib.sha256((current['username'] + ':' + current['password_hash']).encode()).hexdigest()


class DAVDomainController(BaseDomainController):
    def get_domain_realm(self, path_info, environ):
        return 'ImageDAV'

    def require_authentication(self, realm, environ):
        return True

    def basic_auth_user(self, realm, user_name, password, environ):
        return verify_credentials(user_name, password)

    def supports_http_digest_auth(self):
        return False
