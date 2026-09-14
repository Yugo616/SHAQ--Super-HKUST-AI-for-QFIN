"""Pinned native locator and full-cache verification shared by installed entrypoints."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import stat
import sys
import zipfile


WINDOWS_DEFAULT_FILE_ATTRIBUTES = (0, 0x20)  # DOS default / archive bit, no access mode.
CANONICAL_REGULAR_FILE_ATTRIBUTES = (stat.S_IFREG | 0o644) << 16


def package_content_sha256(file):
    """Container-independent identity, including Unix file type/mode and links."""
    rows=[];seen=set()
    with zipfile.ZipFile(file) as archive:
        for entry in archive.infolist():
            name=entry.filename.rstrip('/')
            if (not name or name in seen or '\\' in name or '\x00' in name
                    or name.startswith('/') or ':' in name
                    or any(part in ('', '.', '..') for part in name.split('/'))):
                raise ValueError('Unsafe or duplicate native package path')
            seen.add(name)
            kind=stat.S_IFMT(entry.external_attr >> 16)
            if entry.is_dir():
                if kind not in (0,stat.S_IFDIR):raise ValueError('Invalid native package directory type')
                rows.append([name+'/',entry.external_attr,hashlib.sha256(b'').hexdigest()])
                continue
            if kind not in (0,stat.S_IFREG,stat.S_IFLNK):raise ValueError('Invalid native package entry type')
            if kind==stat.S_IFLNK or name.endswith('.__symlink'):
                target=archive.read(entry).decode('utf-8')
                resolved=posixpath.normpath(posixpath.join(str(PurePosixPath(name).parent),target))
                prefix='/'.join(name.split('/')[:2])+'/'
                if target.startswith('/') or '\\' in target or '\x00' in target or ':' in target or not prefix.startswith('lib/') or not resolved.startswith(prefix):
                    raise ValueError('Native package symlink leaves application payload')
            with archive.open(entry) as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
            attributes=entry.external_attr
            # .NET Windows CreateEntry defaults become Rust zip's regular 0644 on
            # native delta reconstruction (Velopack 1.2.0 fastzip/mod.rs). Only this
            # representation is equivalent; Unix modes, DOS readonly, links and
            # directories retain their complete identity.
            if entry.create_system == 0 and attributes in WINDOWS_DEFAULT_FILE_ATTRIBUTES and not name.endswith('.__symlink'):
                attributes=CANONICAL_REGULAR_FILE_ATTRIBUTES
            rows.append([name,attributes,digest])
    framed=json.dumps(sorted(rows),ensure_ascii=False,separators=(',',':')).encode('utf-8')
    return hashlib.sha256(framed).hexdigest()


def verify_cached(cache, asset, *, required=True, content_sha256=None):
    name = asset.FileName
    if Path(name).name != name or '/' in name or '\\' in name:
        raise ValueError('更新缓存文件名无效')
    file = cache / name
    if not file.exists() and not file.is_symlink() and not required:
        return
    if cache.is_symlink() or file.is_symlink() or not file.is_file():
        raise ValueError('更新缓存路径无效')
    with file.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if file.stat().st_size != asset.Size or digest.lower() != asset.SHA256.lower():
        if content_sha256 and package_content_sha256(file)==content_sha256.lower():
            return
        raise ValueError('完整更新包缓存 SHA256 校验失败；未安装更新')


def native_locator(sdk, package_id, *, cache=None):
    executable = Path(sys.executable).resolve()
    if sys.platform == 'darwin':
        root = next((p for p in executable.parents if p.suffix == '.app'), None)
        if root is None or executable.parent != root/'Contents/MacOS':
            raise RuntimeError('Not an installed native application')
        binary = root/'Contents/MacOS'
        manifest = binary/'sq.version'
        if not manifest.is_file():manifest = root/'Contents/Resources/sq.version'
        updater = binary/'UpdateMac'
        cache = cache or Path.home()/'Library/Caches/velopack'/package_id/'packages'
        portable = True
    elif sys.platform == 'win32':
        binary = executable.parent
        if binary.name != 'current':raise RuntimeError('Not an installed native application')
        root = binary.parent
        manifest,updater = binary/'sq.version',root/'Update.exe'
        cache = cache or root/'packages'
        if not os.access(root, os.W_OK):
            raise RuntimeError('Application installation directory is not writable; use full installer')
        portable = (root/'.portable').exists()
    else:
        raise RuntimeError('Unsupported native updater')
    if not manifest.is_file() or not updater.is_file() or cache.is_symlink():
        raise RuntimeError('Incomplete native installation locator')
    return sdk.VelopackLocatorConfig(root,updater,cache,manifest,binary,portable), cache
