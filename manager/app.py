import os
import re
import shutil
import stat
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

from cleanup import get_state, run_cleanup, set_policy, start_scheduler, stop_scheduler
from fastapi import APIRouter, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.wsgi import WSGIMiddleware
from wsgidav.wsgidav_app import WsgiDAVApp
from wsgidav.dav_error import DAVError, HTTP_FORBIDDEN
from wsgidav.fs_dav_provider import FilesystemProvider

from auth import DAVDomainController, prepare, session_version, settings, update_settings, verify_credentials


@asynccontextmanager
async def lifespan(_app):
    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
admin = APIRouter(prefix='/admin')
DATA = Path('/data/chatgpt2api/images').resolve()
MAX_UPLOAD = 50 * 1024 * 1024
ALLOWED = {'.png', '.jpg', '.jpeg', '.webp', '.gif'}
DATA.mkdir(parents=True, exist_ok=True)
SESSION_SECRET = prepare()


@app.middleware('http')
async def protect_admin(request: Request, call_next):
    path = request.url.path
    public = {'/admin/login', '/admin/api/login'}
    if (path == '/admin' or path.startswith('/admin/')) and path not in public:
        if request.session.get('version') != session_version():
            request.session.clear()
            if path.startswith('/admin/api/'):
                return JSONResponse({'detail': '请先登录'}, status_code=401)
            return RedirectResponse('/admin/login', status_code=303)
    return await call_next(request)


app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET,
                   session_cookie='imagedav_session', max_age=8 * 3600, same_site='lax')


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
    return RedirectResponse('/admin/', status_code=303)


@app.get('/admin')
def admin_redirect():
    return RedirectResponse('/admin/', status_code=303)


@admin.get('/')
def management_home():
    return FileResponse('/app/index.html')


@admin.get('/login')
def login_page():
    return FileResponse('/app/login.html')


class LoginInput(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=1024)


@admin.post('/api/login')
def login(value: LoginInput, request: Request, x_requested_with: str | None = Header(default=None)):
    require_ui_request(x_requested_with)
    if not verify_credentials(value.username, value.password):
        raise HTTPException(401, '用户名或密码错误')
    request.session.clear()
    request.session['version'] = session_version()
    return {'ok': True}


@admin.post('/api/logout')
def logout(request: Request, x_requested_with: str | None = Header(default=None)):
    require_ui_request(x_requested_with)
    request.session.clear()
    return {'ok': True}


@admin.get('/api/settings')
def get_settings():
    current = settings()
    return {'username': current['username'], 'public_base': current['public_base'],
            'webdav_path': '/dav', 'root_path': 'chatgpt2api/images'}


class SettingsInput(BaseModel):
    username: str
    password: str = ''
    public_base: str = ''


@admin.post('/api/settings')
def save_settings(value: SettingsInput, x_requested_with: str | None = Header(default=None)):
    require_ui_request(x_requested_with)
    username = value.username.strip()
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', username):
        raise HTTPException(400, 'Username must contain 1-64 letters, digits, dots, underscores or dashes')
    current = settings()
    if username != current['username'] and not value.password:
        raise HTTPException(400, 'Changing the username requires a new password')
    if value.password and len(value.password) < 8:
        raise HTTPException(400, 'Password must have at least 8 characters')
    base = value.public_base.strip().rstrip('/')
    if base:
        parsed = urlparse(base)
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise HTTPException(400, 'Public address must be an HTTP(S) origin without a path')
    update_settings(username, value.password, base)
    return {'ok': True, 'credentials_changed': bool(value.password)}


@admin.get('/api/files')
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


@admin.get('/api/storage')
def storage_usage():
    disk = shutil.disk_usage(DATA)
    file_count = 0
    file_bytes = 0
    scan_errors = 0

    def scan_error(_error):
        nonlocal scan_errors
        scan_errors += 1

    for directory, dirs, files in os.walk(DATA, followlinks=False, onerror=scan_error):
        dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
        for name in files:
            try:
                info = (Path(directory) / name).lstat()
                if stat.S_ISREG(info.st_mode):
                    file_count += 1
                    file_bytes += info.st_size
            except FileNotFoundError:
                continue  # A concurrent upload, delete or cleanup may change the directory.
            except OSError as exc:
                scan_error(exc)
    return {'disk_total_bytes': disk.total, 'disk_used_bytes': disk.used,
            'disk_free_bytes': disk.free,
            'disk_used_percent': round(disk.used / disk.total * 100, 1) if disk.total else 0,
            'file_count': file_count, 'file_bytes': file_bytes, 'scan_errors': scan_errors,
            'updated_at': datetime.now(timezone.utc).isoformat()}


@admin.post('/api/files')
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


@admin.delete('/api/files')
def delete_file(value: DeleteInput, x_requested_with: str | None = Header(default=None)):
    require_ui_request(x_requested_with)
    target = resolve_path(value.path)
    if not target.is_file() or target.is_symlink():
        raise HTTPException(404, 'File not found')
    target.unlink()
    return {'ok': True}


@admin.get('/api/cleanup')
def cleanup_status():
    return get_state()


class CleanupPolicy(BaseModel):
    enabled: bool
    retention_hours: int = Field(ge=1, le=8760)
    interval_hours: int = Field(ge=1, le=168)


@admin.post('/api/cleanup/policy')
def save_cleanup_policy(value: CleanupPolicy, x_requested_with: str | None = Header(default=None)):
    require_ui_request(x_requested_with)
    return set_policy(value.enabled, value.retention_hours, value.interval_hours)


class CleanupRequest(BaseModel):
    hours: int = Field(ge=1, le=8760)


@admin.post('/api/cleanup/run')
def cleanup_now(value: CleanupRequest, x_requested_with: str | None = Header(default=None)):
    require_ui_request(x_requested_with)
    return run_cleanup(value.hours)


@app.api_route('/images/{relative:path}', methods=['GET', 'HEAD'])
def get_image(relative: str):
    target = resolve_path(relative)
    if not target.is_file() or target.name.startswith('.'):
        raise HTTPException(404, 'Image not found')
    return FileResponse(target, headers={'X-Content-Type-Options': 'nosniff'})


app.include_router(admin)


class ImageDAVProvider(FilesystemProvider):
    def _loc_to_file_path(self, path, environ=None):
        if '..' in path.split('/') or '\\' in path:
            raise DAVError(HTTP_FORBIDDEN, 'Invalid path')
        target = Path(super()._loc_to_file_path(path, environ))
        root = Path(self.root_folder_path)
        if not target.resolve().is_relative_to(root):
            raise DAVError(HTTP_FORBIDDEN, 'Path is outside the data directory')
        current = root
        for part in target.relative_to(root).parts:
            current /= part
            if current.is_symlink():
                raise DAVError(HTTP_FORBIDDEN, 'Symbolic links are not supported')
        return str(target)


app.mount('/dav', WSGIMiddleware(WsgiDAVApp({
    'provider_mapping': {'/': ImageDAVProvider('/data', fs_opts={'follow_symlinks': False})},
    'http_authenticator': {'domain_controller': DAVDomainController,
                           'accept_basic': True, 'accept_digest': False, 'default_to_digest': False},
    'dir_browser': {'enable': False},
    'verbose': 1,
})))
