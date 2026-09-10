# Third-party software

SHAQ itself remains unlicensed; this document grants no license to the project or market data.
The new account execution path uses Zipline-reloaded 3.1.1 (Apache-2.0), not Backtrader.
Old Backtrader results remain saved-only history. The historical GPL text is retained in
src/shaq_daily_oracle/licenses/Backtrader-LICENSE.txt; it is not evidence that the new runtime includes that engine.

Native builds use unchanged PyTables 3.11.1 source with optional LZO unavailable.
Its unsupported _comp_lzo stub remains intact. Acceptance requires both runtime LZO
absence and an audit of every native payload's library links; no LZO-linked binaries
or GPL libraries are removed to conceal a dependency. PyTables uses BSD-3-Clause;
HDF5 and bundled compression libraries retain their own notices.

Windows HDF5 build copies receive standard C `#line` directives to keep diagnostic
filenames source-relative while preserving original line numbers and following source
bytes. Generated HDF5 build-settings path strings are normalized on both platforms.
Original source archives and license texts remain unchanged. Windows payloads include
`third-party/hdf5/diagnostic-map.json` with the original source hash, transformation
description and affected file list; the reproducible recipe is in `packaging/build_native.py`.

Each artifact contains third-party/manifest.json with the source commit, architecture,
Python version, exact package versions and bundled-method file hashes. Complete
license/notice texts, Python license, and source-manifest URLs and SHA-256 values accompany
it. Build artifacts retain exact native source archives, repaired wheels and wheel hashes.
All installed third-party distributions are inventoried, including build tools, so this
is deliberately broader than only imported packages. Missing license text fails the build.

pywebview is BSD-3-Clause. PyInstaller is GPL with its bootloader distribution exception;
its license and exception text remain included. QuickJS has an MIT license; source/native
runtime notices are included. NumPy/SciPy wheel notices cover bundled numerical libraries.
Windows MinGW runtime notices must be reviewed from the actual Windows payload before release.

Two upstream packaging irregularities are handled explicitly: iso4217 dedicates its package
to the public domain in PKG-INFO and provides no separate license file. proxy_tools 0.1.0
metadata says MIT, but its source credits Armin Ronacher and Jonathan Tushman and says BSD.
The original BSD text from [its upstream license commit](https://github.com/jtushman/proxy_tools/blob/ccd35a569b95bec271a59890780c7821756c548a/LICENSE.txt)
is retained under packaging/licenses and copied into the payload alongside original metadata.
The project does not resolve this upstream inconsistency by relabeling the code.

The builder omits pip's machine-local direct_url.json installation locator from frozen
payloads, while preserving source URLs and hashes in the public provenance manifest.
Ordinary debug-symbol removal and compiler file-prefix mapping remove private builder paths;
no library, executable code, copyright notice or license is stripped.

References: [PyTables build options](https://www.pytables.org/usersguide/installation.html),
[Zipline-reloaded](https://github.com/stefan-jansen/zipline-reloaded),
[pywebview installation](https://pywebview.flowrl.com/guide/installation.html),
[PyInstaller license and exception](https://pyinstaller.org/en/stable/license.html).
