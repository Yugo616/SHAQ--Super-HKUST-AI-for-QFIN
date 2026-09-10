"""Build unchanged CPython 3.13 native sources; retain archives and repaired wheels."""
from pathlib import Path
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile


def run(*args, **kwargs):
    subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def map_build_metadata(path, prefixes):
    """Normalize generated configuration text; never rewrite upstream source or notices."""
    content = path.read_text(encoding='utf-8')
    for prefix in prefixes:
        content = content.replace(prefix.replace('\\', '\\\\'), '/shaq-build')
        content = content.replace(prefix, '/shaq-build')
    path.write_text(content, encoding='utf-8')


def reset_wheel_directories(output):
    """Discard only this builder's generated wheels; preserve sources and archives."""
    paths = (output / 'raw-wheels', output / 'wheels')
    if any(path.is_symlink() for path in paths):
        raise RuntimeError('Generated wheel directories must not be symlinks')
    for path in paths:
        if path.exists():
            shutil.rmtree(path)
        path.mkdir()
    return paths


def build_bcolz(root, source, wheels, environment):
    # bcolz's Cython<3.2 build requirement conflicts with the PyTables toolchain.
    # Keep all upstream build requirements in a separate CPython 3.13 environment.
    run(sys.executable, '-m', 'venv', environment)
    python = environment / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    run(python, '-m', 'pip', 'install', '--no-cache-dir', '-r', root / 'packaging/bcolz-build.lock.txt')
    run(python, '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation', '--no-cache-dir', '-w', wheels, source)


def verify_windows_blosc2(wheel):
    with zipfile.ZipFile(wheel) as archive:
        names = {name.lower() for name in archive.namelist()}
    if not names & {'tables/libblosc2.dll', 'tables.libs/libblosc2.dll'}:
        raise RuntimeError('Repaired PyTables wheel lacks the Blosc2 filename its loader requires')


def main():
    if sys.version_info[:2] != (3, 13):
        raise RuntimeError('CPython 3.13 is required; do not substitute older binaries')
    root = Path(__file__).resolve().parents[1]
    output = root / 'build/native-dependencies'
    source_dir = output / 'sources'
    source_dir.mkdir(parents=True, exist_ok=True)
    sources = {}
    specification = json.loads((root / 'packaging/native-sources.json').read_text(encoding="utf-8"))
    for name, item in specification.items():
        archive = source_dir / item['url'].rsplit('/', 1)[-1]
        if not archive.exists():
            urllib.request.urlretrieve(item['url'], archive)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != item['sha256']:
            raise RuntimeError(f'Wrong source SHA-256: {archive.name}')
        with tarfile.open(archive) as tar:
            top = Path(tar.getnames()[0]).parts[0]
            if not (source_dir / top).is_dir():
                tar.extractall(source_dir, filter='data')
        sources[name] = source_dir / top
    pip = [sys.executable, '-m', 'pip']
    lock = root / 'packaging/requirements.lock.txt'
    run(*pip, 'install', '--no-cache-dir', '-c', lock, 'setuptools', 'wheel', 'numpy', 'Cython',
        'blosc2', 'packaging', 'py-cpuinfo', 'delvewheel' if sys.platform == 'win32' else 'delocate')
    prefix = output / 'hdf5'
    cmake_build = output / 'hdf5-build'
    deployment = os.environ.get('MACOSX_DEPLOYMENT_TARGET', '15.0')
    run('cmake', '-S', sources['hdf5'], '-B', cmake_build,
        '-DCMAKE_BUILD_TYPE=Release', f'-DCMAKE_INSTALL_PREFIX={prefix}', '-DBUILD_SHARED_LIBS=ON',
        '-DBUILD_TESTING=OFF', '-DHDF5_BUILD_TOOLS=OFF', '-DHDF5_BUILD_EXAMPLES=OFF',
        '-DHDF5_ENABLE_EMBEDDED_LIBINFO=OFF',
        '-DHDF5_BUILD_HL_LIB=ON', '-DHDF5_ENABLE_Z_LIB_SUPPORT=OFF', '-DHDF5_ENABLE_SZIP_SUPPORT=OFF',
        *([] if sys.platform == 'win32' else [f'-DCMAKE_C_FLAGS=-ffile-prefix-map={Path.home()}=/shaq-build',
          f'-DCMAKE_OSX_ARCHITECTURES={platform.machine()}', f'-DCMAKE_OSX_DEPLOYMENT_TARGET={deployment}']))
    map_build_metadata(cmake_build / 'src/H5build_settings.c', [str(root.parent), str(Path.home())])
    run('cmake', '--build', cmake_build, '--config', 'Release', '--parallel', '3')
    run('cmake', '--install', cmake_build, '--config', 'Release')
    empty = output / 'empty-lzo'
    empty.mkdir(exist_ok=True)
    env = dict(os.environ)
    for key in ('CONDA_PREFIX', 'CPATH', 'C_INCLUDE_PATH', 'CPPFLAGS', 'CFLAGS', 'LDFLAGS'):
        env.pop(key, None)
    env.update(HDF5_DIR=str(prefix), LZO_DIR=str(empty), USE_PKGCONFIG='FALSE', DISABLE_AVX2='True')
    if sys.platform != 'win32':
        env['CFLAGS'] = shlex.quote('-ffile-prefix-map=' + str(Path.home()) + '=/shaq-build')
        env['ARCHFLAGS'] = '-arch ' + platform.machine()
        env['MACOSX_DEPLOYMENT_TARGET'] = deployment
        import blosc2
        env['DYLD_LIBRARY_PATH'] = str(prefix / 'lib') + ':' + str(Path(blosc2.__file__).parent / 'lib')
    raw, repaired = reset_wheel_directories(output)
    # Rebuild only our generated intermediate output; never reuse a universal2 cache.
    if (sources['tables'] / 'build').is_dir():
        shutil.rmtree(sources['tables'] / 'build')
    run(*pip, 'wheel', '--no-deps', '--no-build-isolation', '--no-cache-dir', '-w', raw, sources['tables'], env=env)
    if sys.platform == 'win32':
        # bcolz uses MSVC; QuickJS upstream requires 64-bit MinGW-W64 and static pthread.
        with tempfile.TemporaryDirectory(prefix='bcolz-build-', dir=output) as environment:
            build_bcolz(root, sources['bcolz-zipline'], raw, Path(environment))
        run(sys.executable, 'setup.py', 'build', '--compiler=mingw32', 'bdist_wheel', '--dist-dir', raw, cwd=sources['quickjs'])
        import blosc2
        library_dirs = [prefix / 'bin', Path(blosc2.__file__).parent / 'lib', Path(blosc2.__file__).parent / 'bin']
        for wheel in raw.glob('*.whl'):
            run(sys.executable, '-m', 'delvewheel', 'repair', '--add-path',
                os.pathsep.join(str(p) for p in library_dirs if p.is_dir()),
                *(['--no-mangle', 'libblosc2.dll'] if wheel.name.startswith('tables-') else []),
                '-w', repaired, wheel)
        for wheel in repaired.glob('tables-*.whl'):
            verify_windows_blosc2(wheel)
    else:
        for wheel in raw.glob('*.whl'):
            run(sys.executable, '-m', 'wheel', 'tags', '--remove', '--platform-tag',
                'macosx_' + deployment.replace('.', '_') + '_' + platform.machine(), wheel)
        for wheel in raw.glob('*.whl'):
            run(sys.executable, '-m', 'delocate.cmd.delocate_wheel', '--require-archs', platform.machine(), '-w', repaired, wheel, env=env)
    run(*pip, 'install', '--no-deps', '--force-reinstall', *sorted(repaired.glob('*.whl')))
    run(*pip, 'install', '--no-cache-dir', '-r', lock)
    run(*pip, 'install', '--no-deps', '-e', root)
    run(*pip, 'check')
    run(sys.executable, '-c', "import tables; assert tables.which_lib_version('lzo') is None; print(tables.get_hdf5_version())")
    (output / 'wheel-sha256.json').write_text(json.dumps({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in repaired.glob('*.whl')}, indent=2))


if __name__ == '__main__':
    main()
