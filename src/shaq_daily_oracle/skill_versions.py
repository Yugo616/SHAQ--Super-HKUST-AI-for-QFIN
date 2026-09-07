from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .hashing import sha256_file, sha256_payload


class SkillVersionError(ValueError):
    """A remote or local Skill version violates the collaboration boundary."""


SKILL_NAMES = {
    "daily-oracle",
    "market-common-shock",
    "pit-peer-spillover",
    "primary-event-reasoner",
    "capital-order-flow",
    "derivatives-evidence",
    "price-volume-structure",
    "thesis-adversary",
}
ALLOWED_SKILL_RELATIVE_PATHS = {
    f"skills/{name}/SKILL.md" for name in SKILL_NAMES
} | {
    f"skills/{name}/references/foundations.md" for name in SKILL_NAMES
}
MAX_SKILL_FILE_BYTES = 200_000
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[opusr]_[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
LOCAL_PATH_PATTERNS = (
    re.compile(r"/" + r"Users/[^/\s]+/"),
    re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+\\"),
)


def _safe_component(value: str, *, name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-").lower()
    if not normalized or normalized in {"main", "master", "head"}:
        raise SkillVersionError(f"invalid {name}")
    return normalized


def validate_skill_text(relative_path: str, content: str) -> None:
    if relative_path not in ALLOWED_SKILL_RELATIVE_PATHS:
        raise SkillVersionError(f"Skill package path is not allowed: {relative_path}")
    encoded = content.encode("utf-8")
    if not encoded or len(encoded) > MAX_SKILL_FILE_BYTES:
        raise SkillVersionError("Skill file is empty or exceeds the size limit")
    if "\x00" in content:
        raise SkillVersionError("Skill file contains a NUL byte")
    if any(pattern.search(content) for pattern in SECRET_PATTERNS):
        raise SkillVersionError("Skill file appears to contain a credential")
    if any(pattern.search(content) for pattern in LOCAL_PATH_PATTERNS):
        raise SkillVersionError("Skill file contains a local absolute path")
    if relative_path.endswith("/SKILL.md"):
        lines = content.splitlines()
        if len(lines) > 150:
            raise SkillVersionError("Skill file exceeds the governed 150-line limit")
        if not lines or lines[0] != "---" or lines.count("---") < 2:
            raise SkillVersionError("Skill file frontmatter is invalid")
        closing = lines[1:].index("---") + 1
        keys = {
            line.split(":", 1)[0].strip()
            for line in lines[1:closing]
            if ":" in line
        }
        if keys != {"name", "description"}:
            raise SkillVersionError("Skill frontmatter must contain only name and description")


@dataclass(frozen=True)
class SkillVersionManifest:
    version_id: str
    author: str
    base_main_sha: str
    changed_domains: tuple[str, ...]
    created_at: str
    skill_hashes: dict[str, str]
    schema_version: int
    description: str

    @classmethod
    def create(
        cls,
        *,
        version_id: str,
        author: str,
        base_main_sha: str,
        files: dict[str, str],
        description: str,
        created_at: str | None = None,
    ) -> "SkillVersionManifest":
        safe_version = _safe_component(version_id, name="version id")
        safe_author = _safe_component(author, name="author")
        if not re.fullmatch(r"[0-9a-f]{40}", base_main_sha):
            raise SkillVersionError("base main SHA must be a full Git commit SHA")
        if not description.strip() or len(description.strip()) > 500:
            raise SkillVersionError("version description is required and must be concise")
        for path, content in files.items():
            validate_skill_text(path, content)
        domains = tuple(sorted({PurePosixPath(path).parts[1] for path in files}))
        if not domains:
            raise SkillVersionError("Shadow version has no changed Skill files")
        return cls(
            version_id=safe_version,
            author=safe_author,
            base_main_sha=base_main_sha,
            changed_domains=domains,
            created_at=created_at or datetime.now(ZoneInfo("UTC")).isoformat(),
            skill_hashes={
                path: hashlib.sha256(content.encode("utf-8")).hexdigest()
                for path, content in sorted(files.items())
            },
            schema_version=1,
            description=description.strip(),
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SkillVersionManifest":
        required = {
            "version_id", "author", "base_main_sha", "changed_domains", "created_at",
            "skill_hashes", "schema_version", "description",
        }
        if set(value) != required:
            raise SkillVersionError("Shadow manifest fields differ from the supported schema")
        manifest = cls(
            version_id=str(value["version_id"]),
            author=str(value["author"]),
            base_main_sha=str(value["base_main_sha"]),
            changed_domains=tuple(str(item) for item in value["changed_domains"]),
            created_at=str(value["created_at"]),
            skill_hashes={str(key): str(item) for key, item in value["skill_hashes"].items()},
            schema_version=int(value["schema_version"]),
            description=str(value["description"]),
        )
        if manifest.schema_version != 1:
            raise SkillVersionError("unsupported Shadow manifest schema")
        if manifest.version_id != _safe_component(manifest.version_id, name="version id"):
            raise SkillVersionError("Shadow manifest has an invalid version id")
        if manifest.author != _safe_component(manifest.author, name="author"):
            raise SkillVersionError("Shadow manifest has an invalid author")
        if not re.fullmatch(r"[0-9a-f]{40}", manifest.base_main_sha):
            raise SkillVersionError("Shadow manifest has an invalid base commit")
        for path, digest in manifest.skill_hashes.items():
            if path not in ALLOWED_SKILL_RELATIVE_PATHS or not re.fullmatch(
                r"[0-9a-f]{64}", digest
            ):
                raise SkillVersionError("Shadow manifest has an invalid Skill hash")
        expected_domains = tuple(sorted({
            PurePosixPath(path).parts[1] for path in manifest.skill_hashes
        }))
        if manifest.changed_domains != expected_domains:
            raise SkillVersionError("Shadow manifest domains do not match its Skill files")
        if not manifest.description.strip() or len(manifest.description.strip()) > 500:
            raise SkillVersionError("Shadow manifest description is invalid")
        return manifest

    def as_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "author": self.author,
            "base_main_sha": self.base_main_sha,
            "changed_domains": list(self.changed_domains),
            "created_at": self.created_at,
            "skill_hashes": dict(sorted(self.skill_hashes.items())),
            "schema_version": self.schema_version,
            "description": self.description,
        }

    def identity(self) -> str:
        return sha256_payload(self.as_dict())


@dataclass(frozen=True)
class GitHubRepositoryConfig:
    owner: str
    repository: str
    client_id: str
    branch_prefix: str = "shadow/"
    skill_package_root: str = "shadow_versions"
    token_refresh_leeway_seconds: int = 300
    api_base_url: str = "https://api.github.com"
    web_base_url: str = "https://github.com"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "GitHubRepositoryConfig":
        try:
            config = cls(
                owner=str(value.get("owner", "")),
                repository=str(value.get("repository", "")),
                client_id=str(value.get("github_app_client_id", value.get("client_id", ""))),
                branch_prefix=str(value.get("branch_prefix", "shadow/")),
                skill_package_root=str(value.get("skill_package_root", "shadow_versions")),
                token_refresh_leeway_seconds=int(
                    value.get("token_refresh_leeway_seconds", 300)
                ),
                api_base_url=str(value.get("api_base_url", "https://api.github.com")),
                web_base_url=str(value.get("web_base_url", "https://github.com")),
            )
        except (TypeError, ValueError) as exc:
            raise SkillVersionError("GitHub repository configuration is invalid") from exc
        if not config.owner or not config.repository:
            raise SkillVersionError("team repository is not configured")
        if not config.branch_prefix.endswith("/"):
            raise SkillVersionError("personal branch prefix must end with a slash")
        if config.token_refresh_leeway_seconds <= 0:
            raise SkillVersionError("GitHub token refresh leeway must be positive")
        if PurePosixPath(config.skill_package_root).is_absolute() or ".." in PurePosixPath(
            config.skill_package_root
        ).parts:
            raise SkillVersionError("Skill package root is invalid")
        return config

    def personal_branch(self, login: str) -> str:
        return self.branch_prefix + _safe_component(login, name="GitHub login")


class GitHubSkillClient:
    """Least-privilege REST client; no local Git executable is required."""

    def __init__(self, *, config: GitHubRepositoryConfig, token: str = "") -> None:
        self.config = config
        self.token = token.strip()

    @staticmethod
    def _httpx():
        try:
            import httpx  # type: ignore
        except ImportError as exc:
            raise SkillVersionError("httpx is unavailable") from exc
        return httpx

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "SHAQ-Daily-Oracle-Lab",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        httpx = self._httpx()
        try:
            response = httpx.request(
                method,
                self.config.api_base_url.rstrip("/") + path,
                headers=self._headers(),
                timeout=30,
                **kwargs,
            )
            response.raise_for_status()
            return response.json() if response.content else None
        except Exception as exc:
            raise SkillVersionError(
                f"GitHub request failed: {type(exc).__name__}: {exc}"
            ) from exc

    def begin_device_flow(self) -> dict[str, Any]:
        if not self.config.client_id:
            raise SkillVersionError("GitHub App client id is not configured in this build")
        httpx = self._httpx()
        try:
            response = httpx.post(
                self.config.web_base_url.rstrip("/") + "/login/device/code",
                data={"client_id": self.config.client_id},
                headers={"Accept": "application/json", "User-Agent": "SHAQ-Daily-Oracle-Lab"},
                timeout=30,
            )
            response.raise_for_status()
            value = response.json()
        except Exception as exc:
            raise SkillVersionError(
                f"GitHub device login could not start: {type(exc).__name__}: {exc}"
            ) from exc
        required = {"device_code", "user_code", "verification_uri", "expires_in", "interval"}
        if not isinstance(value, dict) or not required <= set(value):
            raise SkillVersionError("GitHub returned an invalid device login response")
        return {key: value[key] for key in sorted(required)}

    def poll_device_flow(self, device_code: str) -> dict[str, Any]:
        if not self.config.client_id:
            raise SkillVersionError("GitHub App client id is not configured in this build")
        httpx = self._httpx()
        try:
            response = httpx.post(
                self.config.web_base_url.rstrip("/") + "/login/oauth/access_token",
                data={
                    "client_id": self.config.client_id,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                headers={"Accept": "application/json", "User-Agent": "SHAQ-Daily-Oracle-Lab"},
                timeout=30,
            )
            response.raise_for_status()
            value = response.json()
        except Exception as exc:
            raise SkillVersionError(
                f"GitHub device login check failed: {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise SkillVersionError("GitHub returned an invalid device login result")
        if value.get("error"):
            return {"status": str(value["error"]), "interval": value.get("interval")}
        return self._validated_token_result(value)

    def refresh_user_token(self, refresh_token: str) -> dict[str, Any]:
        token = refresh_token.strip()
        if not self.config.client_id:
            raise SkillVersionError("GitHub App client id is not configured in this build")
        if not token:
            raise SkillVersionError("GitHub login refresh token is unavailable")
        httpx = self._httpx()
        try:
            response = httpx.post(
                self.config.web_base_url.rstrip("/") + "/login/oauth/access_token",
                data={
                    "client_id": self.config.client_id,
                    "refresh_token": token,
                    "grant_type": "refresh_token",
                },
                headers={"Accept": "application/json", "User-Agent": "SHAQ-Daily-Oracle-Lab"},
                timeout=30,
            )
            response.raise_for_status()
            value = response.json()
        except Exception as exc:
            raise SkillVersionError(
                f"GitHub login refresh failed: {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(value, dict) or value.get("error"):
            error = str(value.get("error", "invalid response")) if isinstance(value, dict) else "invalid response"
            raise SkillVersionError(f"GitHub login refresh failed: {error}")
        return self._validated_token_result(value)

    @staticmethod
    def _validated_token_result(value: dict[str, Any]) -> dict[str, Any]:
        token = str(value.get("access_token", "")).strip()
        if not token:
            raise SkillVersionError("GitHub device login returned no token")
        result: dict[str, Any] = {
            "status": "authorized",
            "token": token,
            "scope": value.get("scope", ""),
        }
        expiring_fields = {
            "expires_in", "refresh_token", "refresh_token_expires_in"
        }
        if expiring_fields.intersection(value):
            try:
                expires_in = int(value["expires_in"])
                refresh_expires_in = int(value["refresh_token_expires_in"])
            except (KeyError, TypeError, ValueError) as exc:
                raise SkillVersionError(
                    "GitHub returned incomplete expiring-token metadata"
                ) from exc
            refresh_token = str(value.get("refresh_token", "")).strip()
            if expires_in <= 0 or refresh_expires_in <= 0 or not refresh_token:
                raise SkillVersionError(
                    "GitHub returned incomplete expiring-token metadata"
                )
            result.update({
                "expires_in": expires_in,
                "refresh_token": refresh_token,
                "refresh_token_expires_in": refresh_expires_in,
            })
        return result

    def authenticated_user(self) -> dict[str, Any]:
        if not self.token:
            raise SkillVersionError("GitHub login is required")
        value = self._request("GET", "/user")
        login = str(value.get("login", "")) if isinstance(value, dict) else ""
        if not login:
            raise SkillVersionError("GitHub user response has no login")
        return {"login": login, "name": value.get("name"), "avatar_url": value.get("avatar_url")}

    def repository_access(self) -> dict[str, bool]:
        value = self._request("GET", self._repo_path(""))
        if not isinstance(value, dict):
            raise SkillVersionError("GitHub repository response is invalid")
        permissions = value.get("permissions", {})
        can_read = bool(value.get("id"))
        can_write = bool(
            isinstance(permissions, dict)
            and (permissions.get("push") or permissions.get("maintain") or permissions.get("admin"))
        )
        return {"read": can_read, "write": can_write}

    def _repo_path(self, suffix: str) -> str:
        return f"/repos/{self.config.owner}/{self.config.repository}{suffix}"

    def ref_sha(self, branch: str) -> str | None:
        try:
            value = self._request("GET", self._repo_path(f"/git/ref/heads/{branch}"))
        except SkillVersionError as exc:
            if "404" in str(exc):
                return None
            raise
        sha = value.get("object", {}).get("sha") if isinstance(value, dict) else None
        return str(sha) if sha else None

    def ensure_personal_branch(self, login: str) -> str:
        branch = self.config.personal_branch(login)
        if self.ref_sha(branch):
            return branch
        main_sha = self.ref_sha("main")
        if not main_sha:
            raise SkillVersionError("team repository main branch is unavailable")
        self._request(
            "POST",
            self._repo_path("/git/refs"),
            json={"ref": f"refs/heads/{branch}", "sha": main_sha},
        )
        return branch

    def list_shadow_branches(self) -> list[dict[str, Any]]:
        value = self._request("GET", self._repo_path("/branches?per_page=100"))
        if not isinstance(value, list):
            raise SkillVersionError("GitHub branch response is invalid")
        return [
            {"name": str(row["name"]), "sha": str(row["commit"]["sha"])}
            for row in value
            if isinstance(row, dict)
            and str(row.get("name", "")).startswith(self.config.branch_prefix)
        ]

    def get_content(self, path: str, *, ref: str) -> bytes:
        pure = PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts:
            raise SkillVersionError("remote content path is invalid")
        value = self._request("GET", self._repo_path(
            f"/contents/{quote(pure.as_posix(), safe='/')}?ref={quote(ref, safe='')}"
        ))
        if not isinstance(value, dict) or value.get("encoding") != "base64":
            raise SkillVersionError("GitHub content response is invalid")
        return base64.b64decode(str(value.get("content", "")), validate=True)

    def list_directory(self, path: str, *, ref: str) -> list[dict[str, Any]]:
        pure = PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts:
            raise SkillVersionError("remote directory path is invalid")
        value = self._request("GET", self._repo_path(
            f"/contents/{quote(pure.as_posix(), safe='/')}?ref={quote(ref, safe='')}"
        ))
        if not isinstance(value, list):
            return []
        return [
            {"name": str(row.get("name", "")), "path": str(row.get("path", "")), "type": row.get("type")}
            for row in value if isinstance(row, dict)
        ]

    def content_exists(self, path: str, *, ref: str) -> bool:
        try:
            self.get_content(path, ref=ref)
        except SkillVersionError as exc:
            if "404" in str(exc):
                return False
            raise
        return True

    def upload_version(
        self,
        *,
        login: str,
        manifest: SkillVersionManifest,
        files: dict[str, str],
    ) -> dict[str, Any]:
        if manifest.author != _safe_component(login, name="GitHub login"):
            raise SkillVersionError("manifest author differs from the authenticated user")
        if set(files) != set(manifest.skill_hashes):
            raise SkillVersionError("uploaded files differ from the immutable manifest")
        for path, content in files.items():
            validate_skill_text(path, content)
            if manifest.skill_hashes.get(path) != hashlib.sha256(
                content.encode("utf-8")
            ).hexdigest():
                raise SkillVersionError("manifest Skill hash differs from uploaded bytes")
        branch = self.ensure_personal_branch(login)
        root = PurePosixPath(self.config.skill_package_root) / manifest.author / manifest.version_id
        if self.content_exists((root / "manifest.json").as_posix(), ref=branch):
            raise SkillVersionError(
                "this immutable Shadow version id already exists on the personal branch"
            )
        head = self.ref_sha(branch)
        if not head:
            raise SkillVersionError("personal branch has no head commit")
        commit = self._request("GET", self._repo_path(f"/git/commits/{head}"))
        base_tree = str(commit.get("tree", {}).get("sha", ""))
        if not base_tree:
            raise SkillVersionError("personal branch head has no tree")
        payload_files = {
            (root / "manifest.json").as_posix(): json.dumps(
                manifest.as_dict(), indent=2, sort_keys=True
            ) + "\n",
            **{
                (root / "changed_skills" / PurePosixPath(path).relative_to("skills")).as_posix(): content
                for path, content in files.items()
            },
        }
        entries = []
        for path, content in sorted(payload_files.items()):
            blob = self._request(
                "POST", self._repo_path("/git/blobs"),
                json={"content": content, "encoding": "utf-8"},
            )
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        tree = self._request(
            "POST", self._repo_path("/git/trees"),
            json={"base_tree": base_tree, "tree": entries},
        )
        new_commit = self._request(
            "POST", self._repo_path("/git/commits"),
            json={
                "message": f"Shadow Skill {manifest.version_id}: {manifest.description}",
                "tree": tree["sha"],
                "parents": [head],
            },
        )
        self._request(
            "PATCH", self._repo_path(f"/git/refs/heads/{branch}"),
            json={"sha": new_commit["sha"], "force": False},
        )
        return {"branch": branch, "commit_sha": new_commit["sha"], "manifest": manifest.as_dict()}

    def list_remote_versions(self) -> list[dict[str, Any]]:
        """Discover immutable manifests without executing remote repository content."""

        versions: list[dict[str, Any]] = []
        root = self.config.skill_package_root
        for branch_row in self.list_shadow_branches():
            branch = branch_row["name"]
            branch_author = _safe_component(
                branch.removeprefix(self.config.branch_prefix), name="branch author"
            )
            for author_row in self.list_directory(root, ref=branch):
                if author_row.get("type") != "dir":
                    continue
                author = _safe_component(str(author_row["name"]), name="author")
                if author != branch_author:
                    continue
                author_path = f"{root}/{author}"
                for version_row in self.list_directory(author_path, ref=branch):
                    if version_row.get("type") != "dir":
                        continue
                    version_id = _safe_component(
                        str(version_row["name"]), name="version id"
                    )
                    manifest_path = f"{author_path}/{version_id}/manifest.json"
                    try:
                        manifest = SkillVersionManifest.from_dict(json.loads(
                            self.get_content(manifest_path, ref=branch).decode("utf-8")
                        ))
                    except (UnicodeDecodeError, json.JSONDecodeError, SkillVersionError):
                        continue
                    if manifest.author != author or manifest.version_id != version_id:
                        continue
                    versions.append({
                        **manifest.as_dict(),
                        "branch": branch,
                        "commit_sha": branch_row["sha"],
                        "manifest_sha256": manifest.identity(),
                    })
        return sorted(
            versions,
            key=lambda row: (
                str(row["created_at"]), str(row["author"]), str(row["version_id"])
            ),
            reverse=True,
        )

    def download_version(
        self, *, branch: str, author: str, version_id: str
    ) -> tuple[SkillVersionManifest, dict[str, str], str]:
        expected_branch = self.config.personal_branch(author)
        if branch != expected_branch:
            raise SkillVersionError("Shadow package is not on its author's personal branch")
        safe_author = _safe_component(author, name="author")
        safe_version = _safe_component(version_id, name="version id")
        root = PurePosixPath(self.config.skill_package_root) / safe_author / safe_version
        try:
            manifest = SkillVersionManifest.from_dict(json.loads(
                self.get_content((root / "manifest.json").as_posix(), ref=branch).decode("utf-8")
            ))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SkillVersionError("remote Shadow manifest is invalid JSON") from exc
        if manifest.author != safe_author or manifest.version_id != safe_version:
            raise SkillVersionError("remote Shadow identity differs from its path")
        files: dict[str, str] = {}
        for relative_path, expected_hash in sorted(manifest.skill_hashes.items()):
            remote = root / "changed_skills" / PurePosixPath(relative_path).relative_to("skills")
            try:
                content = self.get_content(remote.as_posix(), ref=branch).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SkillVersionError("remote Skill is not UTF-8 text") from exc
            validate_skill_text(relative_path, content)
            if hashlib.sha256(content.encode("utf-8")).hexdigest() != expected_hash:
                raise SkillVersionError("remote Skill bytes differ from their manifest")
            files[relative_path] = content
        commit_sha = self.ref_sha(branch)
        if not commit_sha:
            raise SkillVersionError("remote Shadow branch disappeared during download")
        return manifest, files, commit_sha


class LocalSkillRegistry:
    def __init__(self, *, root: Path, package_skills: Path) -> None:
        self.root = root.resolve()
        self.package_skills = package_skills.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def main_version(self) -> dict[str, Any]:
        hashes = {
            path.relative_to(self.package_skills.parent).as_posix(): sha256_file(path)
            for pattern in ("*/SKILL.md", "*/references/foundations.md")
            for path in sorted(self.package_skills.glob(pattern))
        }
        return {
            "version_id": "main",
            "author": "team",
            "label": "main · 正式Skill基准（仅Shadow）",
            "skill_hashes": hashes,
            "version_sha256": sha256_payload(hashes),
            "source": "bundled-main",
        }

    def install(
        self, *, manifest: SkillVersionManifest, files: dict[str, str], commit_sha: str
    ) -> Path:
        if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
            raise SkillVersionError("installed version requires a full Git commit SHA")
        if set(files) != set(manifest.skill_hashes):
            raise SkillVersionError("downloaded files differ from their manifest")
        for relative_path, content in files.items():
            validate_skill_text(relative_path, content)
            if hashlib.sha256(content.encode("utf-8")).hexdigest() != manifest.skill_hashes[
                relative_path
            ]:
                raise SkillVersionError("downloaded Skill differs from its manifest")
        target = (self.root / manifest.author / manifest.version_id).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise SkillVersionError("installed version escapes the local registry") from exc
        if target.exists():
            existing = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
            if existing.get("manifest_sha256") != manifest.identity():
                raise SkillVersionError("an immutable local Skill version already has other bytes")
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".install-", dir=target.parent))
        try:
            for relative_path, content in files.items():
                destination = temporary / "changed_skills" / PurePosixPath(
                    relative_path
                ).relative_to("skills")
                destination.parent.mkdir(parents=True, exist_ok=True)
                # Skill identities are byte hashes shared by Windows and macOS.
                # Writing bytes prevents Windows from translating LF into CRLF.
                destination.write_bytes(content.encode("utf-8"))
            stored = {
                **manifest.as_dict(),
                "manifest_sha256": manifest.identity(),
                "source_commit_sha": commit_sha,
            }
            (temporary / "manifest.json").write_text(
                json.dumps(stored, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            temporary.replace(target)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return target

    def list_versions(self) -> list[dict[str, Any]]:
        versions = [self.main_version()]
        for path in sorted(self.root.glob("*/*/manifest.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                manifest = SkillVersionManifest.from_dict({
                    key: value[key] for key in (
                        "version_id", "author", "base_main_sha", "changed_domains",
                        "created_at", "skill_hashes", "schema_version", "description",
                    )
                })
                if value.get("manifest_sha256") != manifest.identity():
                    continue
                versions.append({
                    **manifest.as_dict(),
                    "label": f"{manifest.author} · {manifest.version_id}",
                    "version_sha256": manifest.identity(),
                    "source": "github-shadow",
                    "source_commit_sha": value.get("source_commit_sha"),
                })
            except Exception:
                continue
        return versions

    def effective_skills(self, version_id: str, author: str = "") -> dict[str, str]:
        output = {
            path.relative_to(self.package_skills.parent).as_posix(): path.read_text(encoding="utf-8")
            for pattern in ("*/SKILL.md", "*/references/foundations.md")
            for path in sorted(self.package_skills.glob(pattern))
        }
        if version_id == "main":
            return output
        target = (self.root / _safe_component(author, name="author") / _safe_component(
            version_id, name="version id"
        )).resolve()
        manifest_path = target / "manifest.json"
        if not manifest_path.is_file():
            raise SkillVersionError("local Skill version is not installed")
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = SkillVersionManifest.from_dict({
            key: value[key] for key in (
                "version_id", "author", "base_main_sha", "changed_domains", "created_at",
                "skill_hashes", "schema_version", "description",
            )
        })
        for relative_path, expected in manifest.skill_hashes.items():
            source = target / "changed_skills" / PurePosixPath(relative_path).relative_to("skills")
            if not source.is_file() or sha256_file(source) != expected:
                raise SkillVersionError("installed Skill bytes failed their manifest hash")
            output[relative_path] = source.read_text(encoding="utf-8")
        return output

    def save_draft(
        self, *, author: str, draft_id: str, files: dict[str, str], base_main_sha: str
    ) -> Path:
        safe_author = _safe_component(author, name="author")
        safe_draft = _safe_component(draft_id, name="draft id")
        root = (self.root / "drafts" / safe_author / safe_draft).resolve()
        try:
            root.relative_to(self.root)
        except ValueError as exc:
            raise SkillVersionError("draft escapes the local registry") from exc
        root.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{safe_draft}-", dir=root.parent))
        backup = root.with_name(f".{safe_draft}.previous")
        try:
            hashes = {}
            for path, content in files.items():
                validate_skill_text(path, content)
                destination = staging / PurePosixPath(path).relative_to("skills")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content.encode("utf-8"))
                hashes[path] = hashlib.sha256(content.encode("utf-8")).hexdigest()
            (staging / "draft.json").write_text(json.dumps({
                "author": safe_author,
                "draft_id": safe_draft,
                "base_main_sha": base_main_sha,
                "skill_hashes": hashes,
            }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            if root.exists():
                if backup.exists():
                    shutil.rmtree(backup)
                root.replace(backup)
            staging.replace(root)
            if backup.exists():
                shutil.rmtree(backup)
        except Exception:
            if not root.exists() and backup.exists():
                backup.replace(root)
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return root

    def load_draft(self, *, author: str, draft_id: str) -> tuple[dict[str, Any], dict[str, str]]:
        safe_author = _safe_component(author, name="author")
        safe_draft = _safe_component(draft_id, name="draft id")
        root = (self.root / "drafts" / safe_author / safe_draft).resolve()
        try:
            root.relative_to(self.root)
        except ValueError as exc:
            raise SkillVersionError("draft escapes the local registry") from exc
        metadata_path = root / "draft.json"
        if not metadata_path.is_file():
            raise SkillVersionError("local Shadow draft is unavailable")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        required = {"author", "draft_id", "base_main_sha", "skill_hashes"}
        if set(metadata) != required or metadata["author"] != safe_author or metadata[
            "draft_id"
        ] != safe_draft:
            raise SkillVersionError("local Shadow draft identity is invalid")
        if not re.fullmatch(r"[0-9a-f]{40}", str(metadata["base_main_sha"])):
            raise SkillVersionError("local Shadow draft main commit is invalid")
        files: dict[str, str] = {}
        for relative_path, expected in metadata["skill_hashes"].items():
            if relative_path not in ALLOWED_SKILL_RELATIVE_PATHS:
                raise SkillVersionError("local Shadow draft contains a disallowed path")
            source = root / PurePosixPath(relative_path).relative_to("skills")
            if not source.is_file() or sha256_file(source) != expected:
                raise SkillVersionError("local Shadow draft hash mismatch")
            content = source.read_text(encoding="utf-8")
            validate_skill_text(relative_path, content)
            files[relative_path] = content
        return metadata, files

    def list_drafts(self, *, author: str) -> list[dict[str, Any]]:
        safe_author = _safe_component(author, name="author")
        output = []
        for path in sorted((self.root / "drafts" / safe_author).glob("*/draft.json")):
            try:
                metadata, _ = self.load_draft(author=safe_author, draft_id=path.parent.name)
                output.append(metadata)
            except Exception:
                continue
        return output
