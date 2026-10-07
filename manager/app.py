import json
import os
import re
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

from cleanup import get_state, run_cleanup, set_policy, start_scheduler, stop_scheduler
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from passlib.hash import apr_md5_crypt
from pydantic import BaseModel, Field


@asynccontextmanager
async def lifespan(_app):
    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
DATA = Path('/data/chatgpt2api/images').resolve()
CONFIG = Path('/config')
SETTINGS = CONFIG / 'settings.json'
HTPASSWD = CONFIG / 'dav.htpasswd'
MAX_UPLOAD = 50 * 1024 * 1024
ALLOWED = {'.png', '.jpg', '.jpeg', '.webp', '.gif'}


def atomic_write(path: Path, contents: str):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.tmp-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as target:
            target.write(contents)
        os.chmod(name, 0o600 if path == SETTINGS else 0o640)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def settings():
    return json.loads(SETTINGS.read_text(encoding='utf-8'))


def prepare():
    CONFIG.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    if SETTINGS.exists() and HTPASSWD.exists():
        return
    if SETTINGS.exists() != HTPASSWD.exists():
        raise RuntimeError('Incomplete configuration; restore the missing config file from backup')
    username = os.environ.get('INITIAL_USERNAME', '')
    password = os.environ.get('INITIAL_PASSWORD', '')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', username) or len(password) < 12:
        raise RuntimeError('Set INITIAL_USERNAME and INITIAL_PASSWORD (at least 12 characters) in .env')
    atomic_write(HTPASSWD, f'{username}:{apr_md5_crypt.hash(password)}\n')
    atomic_write(SETTINGS, json.dumps({'username': username, 'public_base': ''}))


prepare()


def require_ui_request(marker: str | None):
    if marker != 'image-manager':
        raise HTTPException(403, 'Request must originate from the management UI')


def resolve_path(relative: str) -> Path:
    relative = relative.strip('/')
    parts = relative.split('/') if relative else []
    if any(part in {'.', '..', ''} or '\\' in part for part in parts):
        raise HTTPException(400, 'Invalid path')
    result = DATA.joinpath(*parts).resolve()
    if not result.is_relative_to(DATA):
        raise HTTPException(400, 'Invalid path')
    return result


def public_url(relative: str, public_base: str) -> str:
    encoded = '/'.join(quote(part, safe='') for part in relative.split('/'))
    return f'{public_base.rstrip("/")}/images/{encoded}' if public_base else f'/images/{encoded}'


@app.get('/health')
def health():
    return {'ok': True}


@app.get('/')
def home():
    return FileResponse('/app/index.html')


@app.get('/api/settings')
def get_settings():
    current = settings()
    return {'username': current['username'], 'public_base': current['public_base'],
            'webdav_path': '/dav', 'root_path': 'chatgpt2api/images'}


class SettingsInput(BaseModel):
    username: str
    password: str = ''
    public_base: str = ''


@app.post('/api/settings')
def save_settings(value: SettingsInput, x_requested_with: str | None = Header(default=None)):
    require_ui_request(x_requested_with)
    username = value.username.strip()
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', username):
        raise HTTPException(400, 'Username must contain 1-64 letters, digits, dots, underscores or dashes')
    current = settings()
    if username != current['username'] and not value.password:
        raise HTTPException(400, 'Changing the username requires a new password')
    if value.password and len(value.password) < 12:
        raise HTTPException(400, 'Password must have at least 12 characters')
    base = value.public_base.strip().rstrip('/')
    if base:
        parsed = urlparse(base)
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise HTTPException(400, 'Public address must be an HTTP(S) origin without a path')
    if value.password:
        atomic_write(HTPASSWD, f'{username}:{apr_md5_crypt.hash(value.password)}\n')
    atomic_write(SETTINGS, json.dumps({'username': username, 'public_base': base}))
    return {'ok': True, 'credentials_changed': bool(value.password)}


@app.get('/api/files')
def list_files(path: str = ''):
    directory = resolve_path(path)
    if not directory.is_dir():
        raise HTTPException(404, 'Directory not found')
    current = settings()
    entries = []
    for item in directory.iterdir():
        if item.is_symlink() or (not item.is_dir() and not item.is_file()):
            continue
        rel = item.relative_to(DATA).as_posix()
        metadata = item.stat()
        entries.append({'name': item.name, 'path': rel, 'directory': item.is_dir(),
                        'size': metadata.st_size if item.is_file() else None,
                        'modified': datetime.fromtimestamp(metadata.st_mtime, timezone.utc).isoformat(),
                        'url': public_url(rel, current['public_base']) if item.is_file() else None})
    entries.sort(key=lambda item: (not item['directory'], item['name'].lower()), reverse=False)
    return {'path': path.strip('/'), 'entries': entries[:500], 'total': len(entries)}


@app.post('/api/files')
async def upload_file(file: UploadFile = File(...), path: str = '', x_requested_with: str | None = Header(default=None)):
    require_ui_request(x_requested_with)
    directory = resolve_path(path)
    if not directory.is_dir():
        raise HTTPException(404, 'Directory not found')
    name = Path(file.filename or '').name
    if not name or name in {'.', '..'} or '\\' in name or Path(name).suffix.lower() not in ALLOWED:
        raise HTTPException(400, 'Only PNG, JPEG, WebP and GIF images are allowed')
    target = directory / name
    if target.exists():
        raise HTTPException(409, 'A file with this name already exists')
    fd, temp_name = tempfile.mkstemp(dir=directory, prefix='.upload-')
    try:
        total = 0
        header = b''
        with os.fdopen(fd, 'wb') as output:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_UPLOAD:
                    raise HTTPException(413, 'File exceeds 50 MB')
                if not header:
                    header = chunk[:16]
                output.write(chunk)
        ext = target.suffix.lower()
        valid = (ext == '.png' and header.startswith(b'\x89PNG\r\n\x1a\n') or
                 ext in {'.jpg', '.jpeg'} and header.startswith(b'\xff\xd8\xff') or
                 ext == '.gif' and header.startswith((b'GIF87a', b'GIF89a')) or
                 ext == '.webp' and header.startswith(b'RIFF') and header[8:12] == b'WEBP')
        if not valid:
            raise HTTPException(400, 'File content does not match the image extension')
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return {'ok': True, 'path': target.relative_to(DATA).as_posix()}


class DeleteInput(BaseModel):
    path: str


@app.delete('/api/files')
def delete_file(value: DeleteInput, x_requested_with: str | None = Header(default=None)):
    require_ui_request(x_requested_with)
    target = resolve_path(value.path)
    if not target.is_file() or target.is_symlink():
        raise HTTPException(404, 'File not found')
    target.unlink()
    return {'ok': True}


@app.get('/api/cleanup')
def cleanup_status():
    return get_state()


class CleanupPolicy(BaseModel):
    enabled: bool
    retention_hours: int = Field(ge=1, le=8760)
    interval_hours: int = Field(ge=1, le=168)


@app.post('/api/cleanup/policy')
def save_cleanup_policy(value: CleanupPolicy, x_requested_with: str | None = Header(default=None)):
    require_ui_request(x_requested_with)
    return set_policy(value.enabled, value.retention_hours, value.interval_hours)


class CleanupRequest(BaseModel):
    hours: int = Field(ge=1, le=8760)


@app.post('/api/cleanup/run')
def cleanup_now(value: CleanupRequest, x_requested_with: str | None = Header(default=None)):
    require_ui_request(x_requested_with)
    return run_cleanup(value.hours)
