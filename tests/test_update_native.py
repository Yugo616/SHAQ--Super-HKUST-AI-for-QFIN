import hashlib
import tempfile
import unittest
import zipfile
import stat
from pathlib import Path
from types import SimpleNamespace as NS

from shaq_daily_oracle import update_native


class NativeUpdateTests(unittest.TestCase):
    def test_native_symlink_sidecar_cannot_escape_payload_root(self):
        with tempfile.TemporaryDirectory() as directory:
            file=Path(directory)/'full.nupkg'
            with zipfile.ZipFile(file,'w') as archive:
                archive.writestr('lib/app/Contents/Frameworks/link.__symlink','../../../../outside')
            with self.assertRaises(ValueError):update_native.package_content_sha256(file)

    def test_added_directory_metadata_cannot_bypass_content_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            file=Path(directory)/'full.nupkg'
            with zipfile.ZipFile(file,'w') as archive:archive.writestr('lib/app',b'safe')
            before=update_native.package_content_sha256(file)
            with zipfile.ZipFile(file,'a') as archive:archive.writestr('lib/extra/',b'')
            self.assertNotEqual(before,update_native.package_content_sha256(file))

    def test_content_identity_rejects_traversal_duplicates_and_escaping_links(self):
        with tempfile.TemporaryDirectory() as directory:
            file=Path(directory)/'bad.zip'
            for names in (('../outside',),('lib/app','lib/app')):
                with zipfile.ZipFile(file,'w') as archive:
                    for index,name in enumerate(names):
                        if index:
                            with self.assertWarnsRegex(UserWarning,'Duplicate name'):archive.writestr(name,b'content')
                        else:archive.writestr(name,b'content')
                with self.assertRaises(ValueError):update_native.package_content_sha256(file)
            for target in ('/etc/passwd','../../outside'):
                with zipfile.ZipFile(file,'w') as archive:
                    entry=zipfile.ZipInfo('lib/app');entry.external_attr=(stat.S_IFLNK|0o777)<<16
                    archive.writestr(entry,target)
                with self.assertRaises(ValueError):update_native.package_content_sha256(file)

    def test_permission_changes_change_trusted_content_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            file=Path(directory)/'package.zip';digests=[]
            for mode in (0o644,0o755):
                with zipfile.ZipFile(file,'w') as archive:
                    entry=zipfile.ZipInfo('lib/app');entry.external_attr=(stat.S_IFREG|mode)<<16
                    archive.writestr(entry,b'same content')
                digests.append(update_native.package_content_sha256(file))
            self.assertNotEqual(*digests)

    def test_recompressed_native_delta_requires_identical_content_and_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            full=root/'full.nupkg'; reconstructed=root/'reconstructed.nupkg'
            for path,compression in ((full,zipfile.ZIP_STORED),(reconstructed,zipfile.ZIP_DEFLATED)):
                with zipfile.ZipFile(path,'w',compression=compression) as archive:archive.writestr('lib/app.txt',b'actual program'*100)
            self.assertNotEqual(full.read_bytes(),reconstructed.read_bytes())
            content=update_native.package_content_sha256(full)
            asset=NS(FileName=reconstructed.name,Size=full.stat().st_size,SHA256=hashlib.sha256(full.read_bytes()).hexdigest())
            update_native.verify_cached(root,asset,content_sha256=content)
            with zipfile.ZipFile(reconstructed,'w') as archive:archive.writestr('lib/app.txt',b'corrupted')
            with self.assertRaises(ValueError):update_native.verify_cached(root,asset,content_sha256=content)

    def test_acceptance_paths_reject_normal_installed_application(self):
        from shaq_daily_oracle.update_smoke import acceptance_paths
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with self.assertRaises(ValueError):acceptance_paths(root, Path('/Applications/SHAQ.app/executable'))

    def test_existing_target_cache_is_verified_and_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            cache=Path(directory);data=b'complete package'
            asset=NS(FileName='SHAQ-1-full.nupkg',Size=len(data),SHA256=hashlib.sha256(data).hexdigest())
            file=cache/asset.FileName
            file.write_bytes(b'bad')
            with self.assertRaises(ValueError):update_native.verify_cached(cache,asset)
            file.write_bytes(data);update_native.verify_cached(cache,asset)
            file.unlink();outside=cache/'unrelated';outside.write_bytes(data);file.symlink_to(outside)
            with self.assertRaises(ValueError):update_native.verify_cached(cache,asset)
            self.assertEqual(outside.read_bytes(),data)
