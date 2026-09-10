"""Publish only accepted native CI artifacts, without rebuilding installers."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import tomllib

PLATFORMS = (("Windows-x64", "AMD64", "windows"),
             ("macOS-Apple-Silicon", "arm64", "macos"),
             ("macOS-Intel", "x86_64", "macos"))
METHOD_PREFIXES = ("bundled_versions", "skills", "decision")
SOURCE_PATHS = ("build/third-party", "build/license-sources",
                "build/native-dependencies/sources",
                "build/native-dependencies/wheel-sha256.json")


def gh_api(repo, endpoint):
    return json.loads(subprocess.check_output(["gh", "api", f"repos/{repo}/{endpoint}"]))


def require_absent(repo, endpoint):
    result = subprocess.run(["gh", "api", f"repos/{repo}/{endpoint}"], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode == 0:
        raise RuntimeError(f"Publication target already exists: {endpoint}")
    if "HTTP 404" not in result.stderr and "HTTP 404" not in result.stdout:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Unable to verify publication target absence: {endpoint}: {detail}")


def project_version(repo, source):
    payload = gh_api(repo, f"contents/pyproject.toml?ref={source}")
    if payload.get("encoding") != "base64":
        raise RuntimeError("Unexpected pyproject.toml response encoding")
    return str(tomllib.loads(base64.b64decode(payload["content"]).decode())["project"]["version"])


def method_tree(repo, source):
    payload = gh_api(repo, f"git/trees/{source}?recursive=1")
    if payload.get("truncated"):
        raise RuntimeError(f"Git tree response is truncated for {source}")
    tree = {row["path"]: (row.get("mode", ""), row["type"], row["sha"])
            for row in payload.get("tree", [])
            if any(row["path"] == prefix or row["path"].startswith(prefix + "/")
                   for prefix in METHOD_PREFIXES)}
    for prefix in METHOD_PREFIXES:
        if not any(path == prefix or path.startswith(prefix + "/") for path in tree):
            raise RuntimeError(f"Missing method resource tree: {prefix} at {source}")
    return tree


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_notes(path, version, source):
    path.write_text(f'''SHAQ Daily Oracle Lab {version}

本次更新：当日运行进度、弹出式详情卡片、价格确认修复，以及自动/手动成绩更新。
预测方法与 versions 方法库不变；不会自动开启每日预测。

源码：{source}
三平台共用源码；安装、启动、隔离样例回放、原生界面和卸载由对应 GitHub 原生环境验收。
Windows验收环境为 GitHub Windows Server，不等同逐台 Win10/11 人工测试。
模型连接测试采用固定样例，不宣称所有用户 API 或登录都已实测。
macOS要求15+；Apple Silicon与Intel请选择对应DMG。内部测试包未商业签名/公证。
每个文件旁提供SHA-256，Acceptance.json记录验收；附必要第三方说明和源码。
每日数据、个人账户与密钥不包含在安装包中。
''', encoding="utf-8")


def main():
    source = os.environ["ACCEPTED_SOURCE"]
    run_id = os.environ["ACCEPTED_RUN"]
    version = os.environ["RELEASE_VERSION"]
    repo = os.environ["GH_REPO"]
    expected_base = os.environ["EXPECTED_BASE_SOURCE"]
    expected_versions = os.environ["EXPECTED_VERSIONS_SHA"]
    run = gh_api(repo, f"actions/runs/{run_id}")
    if not (run.get("head_sha") == source and run.get("conclusion") == "success"):
        raise RuntimeError("Accepted run/source or conclusion does not match")
    if run.get("path") != ".github/workflows/build-desktop.yml":
        raise RuntimeError("Accepted run used an unexpected workflow")
    if project_version(repo, source) != version:
        raise RuntimeError("Requested release version does not match accepted pyproject.toml")
    if method_tree(repo, source) != method_tree(repo, expected_base):
        raise RuntimeError("Accepted method resources differ from the expected baseline")
    if gh_api(repo, "git/ref/heads/versions")["object"]["sha"] != expected_versions:
        raise RuntimeError("versions branch moved after acceptance")

    tags = {group: f"lab-v{version}-{group}" for group in ("windows", "macos")}
    for tag in tags.values():
        require_absent(repo, f"git/ref/tags/{tag}")
        require_absent(repo, f"releases/tags/{tag}")

    methods, reports = [], {"windows": [], "macos": []}
    with tempfile.TemporaryDirectory(prefix="shaq-publication-") as temporary:
        staging = Path(temporary)
        assets, notes = {"windows": [], "macos": []}, {}
        for platform, architecture, group in PLATFORMS:
            root = Path("accepted") / f"SHAQ-Daily-Oracle-Lab-{platform}"
            out = staging / group
            out.mkdir(parents=True, exist_ok=True)
            meta = json.loads((root / "build/third-party/manifest.json").read_text())
            if not (meta.get("source_sha") == source and meta.get("source_dirty") is False):
                raise RuntimeError(f"Invalid source metadata for {platform}")
            if str(meta.get("architecture", "")).lower() != architecture.lower():
                raise RuntimeError(f"Invalid architecture metadata for {platform}")
            methods.append(meta["methods"])
            report_root = root / ("dist/diagnostic" if group == "windows" else "dist")
            smoke = json.loads((report_root / "installed-smoke.json").read_text())
            gui = json.loads((report_root / "installed-gui.json").read_text())
            audit = json.loads((report_root / "installed-native-audit.json").read_text())
            if not (smoke.get("status") == "passed" and smoke.get("checks")
                    and all(value is True for value in smoke["checks"].values())):
                raise RuntimeError(f"Invalid installed smoke report for {platform}")
            if not (gui.get("status") == "passed"
                    and set(gui.get("pages", [])) == {"run", "editor", "history"}
                    and gui.get("fixture_replay_loaded") is True
                    and isinstance(gui.get("fixture_candidate_selected"), str)
                    and bool(gui["fixture_candidate_selected"].strip())
                    and gui.get("refresh_preserved_modal") is True
                    and gui.get("refresh_preserved_candidate") is True
                    and gui.get("modal_close_reopen") is True):
                raise RuntimeError(f"Invalid installed GUI report for {platform}")
            if not (audit.get("status") == "passed" and audit.get("failures") == []
                    and isinstance(audit.get("native_count"), int) and audit["native_count"] > 0):
                raise RuntimeError(f"Invalid installed native audit for {platform}")
            if group == "windows":
                delivery = json.loads((report_root / "windows-delivery.json").read_text())
                if not (delivery.get("status") == "passed" and delivery.get("release_promoted") is True
                        and delivery.get("stages")
                        and all(row.get("status") == "passed" for row in delivery["stages"])):
                    raise RuntimeError("Invalid Windows delivery report")

            filename = f"SHAQ-Daily-Oracle-Lab-{platform}" + ("-Setup.exe" if group == "windows" else ".dmg")
            installer = root / "dist" / filename
            sidecar = Path(str(installer) + ".sha256")
            digest = sha256(installer)
            if sidecar.read_text(encoding="utf-8").split() != [digest, filename]:
                raise RuntimeError(f"Invalid checksum sidecar for {platform}")
            staged_installer, staged_sidecar = out / filename, out / sidecar.name
            shutil.copy2(installer, staged_installer)
            shutil.copy2(sidecar, staged_sidecar)
            archive_path = out / f"SHAQ-{platform}-Third-Party-Sources.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                for name in SOURCE_PATHS:
                    archive.add(root / name, arcname=name)
            assets[group].extend((staged_installer, staged_sidecar, archive_path))
            reports[group].append({"platform": platform, "source_sha": source,
                "installer": filename, "sha256": digest,
                "checksum_sidecar": {"filename": sidecar.name, "sha256": sha256(staged_sidecar)},
                "source_archive": {"filename": archive_path.name, "sha256": sha256(archive_path)},
                "installed_checks": smoke["checks"], "native_count": audit["native_count"],
                "gui": gui, "status": "passed",
                "model_calls": "isolated fixtures; not all user providers live-tested"})

        if not methods or any(item != methods[0] for item in methods[1:]):
            raise RuntimeError("Platform method manifests do not match")
        for group in ("windows", "macos"):
            acceptance = staging / group / "Acceptance.json"
            acceptance.write_text(json.dumps({"status": "passed", "run_id": run_id,
                "source_sha": source, "artifacts": reports[group], "methods": methods[0]}, indent=2),
                encoding="utf-8")
            assets[group].append(acceptance)
            notes[group] = staging / f"{group}-notes.md"
            write_notes(notes[group], version, source)

        promoted = []
        try:
            for group in ("windows", "macos"):
                label = "Windows" if group == "windows" else "macOS"
                subprocess.run(["gh", "release", "create", tags[group], "--target", source,
                    "--prerelease", "--draft", "--title", f"SHAQ Daily Oracle Lab {version} · {label}",
                    "--notes-file", str(notes[group]), *[str(path) for path in assets[group]]], check=True)
            for group in ("windows", "macos"):
                subprocess.run(["gh", "release", "edit", tags[group], "--draft=false"], check=True)
                promoted.append(tags[group])
        except Exception:
            for tag in reversed(promoted):
                subprocess.run(["gh", "release", "edit", tag, "--draft=true"], check=False)
            raise


if __name__ == "__main__":
    main()
