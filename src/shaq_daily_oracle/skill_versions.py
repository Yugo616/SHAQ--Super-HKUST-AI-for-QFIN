from __future__ import annotations

import base64
import binascii
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

from .decision_sandbox import run_decision_cases
from .hashing import sha256_file, sha256_payload


class SkillVersionError(ValueError):
    """A remote or local Skill version violates the collaboration boundary."""


def _http_status_code(exc: BaseException) -> int | None:
    """Read an explicit HTTP status without parsing URLs or error prose."""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        status = getattr(current, "status_code", None)
        if isinstance(status, int):
            return status
        response = getattr(current, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status
        current = current.__cause__
    return None


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
} | {
    f"skills/{name}/agents/openai.yaml" for name in SKILL_NAMES
} | {"decision/decision.js", "decision/cases.json"} | {
    f"modules/{domain}/{name}" for domain in ("screening", "market", "relationships", "event", "capital", "derivatives", "price_volume")
    for name in ("compute.js", "cases.json")
}
COMPLETE_METHOD_PATHS = frozenset(ALLOWED_SKILL_RELATIVE_PATHS)
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


def parse_agent_profile(content: str) -> dict[str, Any]:
    """Parse the deliberately small, generated role-card YAML subset.

    Shadow authors edit the three human-facing strings in the desktop form.
    The invocation policy remains a program rule, so a version cannot turn a
    specialist into an implicit top-level Skill by changing prose.
    """
    lines = content.splitlines()
    if len(lines) != 6 or lines[0] != "interface:" or lines[4] != "policy:":
        raise SkillVersionError("agent role card has an unsupported YAML structure")
    expected = (
        ("display_name", "  display_name: ", lines[1]),
        ("short_description", "  short_description: ", lines[2]),
        ("default_prompt", "  default_prompt: ", lines[3]),
    )
    values: dict[str, str] = {}
    for field, prefix, line in expected:
        if not line.startswith(prefix):
            raise SkillVersionError("agent role card fields are incomplete")
        try:
            value = json.loads(line[len(prefix):])
        except json.JSONDecodeError as exc:
            raise SkillVersionError("agent role card strings must be quoted") from exc
        if not isinstance(value, str) or not value.strip() or "\n" in value:
            raise SkillVersionError("agent role card strings must be concise single lines")
        values[field] = value.strip()
    match = re.fullmatch(r"  allow_implicit_invocation: (true|false)", lines[5])
    if not match:
        raise SkillVersionError("agent role card policy is invalid")
    return {
        "display_name": values["display_name"],
        "short_description": values["short_description"],
        "default_prompt": values["default_prompt"],
        "allow_implicit_invocation": match.group(1) == "true",
    }


def render_agent_profile(profile: dict[str, Any]) -> str:
    required = {"display_name", "short_description", "default_prompt", "allow_implicit_invocation"}
    if set(profile) != required or not isinstance(profile["allow_implicit_invocation"], bool):
        raise SkillVersionError("agent role card fields are invalid")
    return (
        "interface:\n"
        f"  display_name: {json.dumps(str(profile['display_name']), ensure_ascii=False)}\n"
        f"  short_description: {json.dumps(str(profile['short_description']), ensure_ascii=False)}\n"
        f"  default_prompt: {json.dumps(str(profile['default_prompt']), ensure_ascii=False)}\n"
        "policy:\n"
        f"  allow_implicit_invocation: {'true' if profile['allow_implicit_invocation'] else 'false'}\n"
    )


def _safe_component(value: str, *, name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-").lower()
    if not normalized or normalized in {"main", "master", "head"}:
        raise SkillVersionError(f"invalid {name}")
    return normalized


def _artifact_domain(relative_path: str) -> str:
    parts = PurePosixPath(relative_path).parts
    return "decision" if parts[0] == "decision" else parts[1]


def _stored_artifact_path(relative_path: str) -> PurePosixPath:
    path = PurePosixPath(relative_path)
    if path.parts[0] == "modules":
        return PurePosixPath("changed_modules") / path.relative_to("modules")
    if path.parts[0] == "skills":
        return PurePosixPath("changed_skills") / path.relative_to("skills")
    return PurePosixPath("changed_decision") / path.relative_to("decision")


def _draft_artifact_path(relative_path: str) -> PurePosixPath:
    path = PurePosixPath(relative_path)
    return path.relative_to("skills") if path.parts[0] == "skills" else path


def validate_artifact_set(files: dict[str, str]) -> dict[str, Any] | None:
    for path, content in files.items():
        validate_skill_text(path, content)
    from .module_rules import MODULES, test_rule
    for module in MODULES:
        paths = {f"modules/{module}/compute.js", f"modules/{module}/cases.json"}
        if paths.intersection(files):
            if not paths.issubset(files):
                raise SkillVersionError("模块代码和固定测试必须一起保存")
            test_rule(files[f"modules/{module}/compute.js"], files[f"modules/{module}/cases.json"])
    decision_paths = {"decision/decision.js", "decision/cases.json"}
    present = decision_paths.intersection(files)
    if present and present != decision_paths:
        raise SkillVersionError(
            "decision.js and cases.json must be saved and uploaded together"
        )
    if present:
        try:
            return run_decision_cases(
                script=files["decision/decision.js"],
                cases_text=files["decision/cases.json"],
            )
        except Exception as exc:
            raise SkillVersionError(f"decision rule tests failed: {exc}") from exc
    return None


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
    if relative_path.endswith("/agents/openai.yaml"):
        profile = parse_agent_profile(content)
        skill_name = PurePosixPath(relative_path).parts[1]
        if profile["allow_implicit_invocation"] != (skill_name == "daily-oracle"):
            raise SkillVersionError("agent role card cannot change invocation policy")
    if relative_path == "decision/cases.json":
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise SkillVersionError("decision cases must be valid JSON") from exc
        if not isinstance(value, dict) or not isinstance(value.get("cases"), list):
            raise SkillVersionError("decision cases require a cases array")


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
    method_name: str = ""
    status_badge: str = ""
    aliases: tuple[str, ...] = ()

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
        method_name: str = "",
        status_badge: str = "",
        aliases: tuple[str, ...] = (),
    ) -> "SkillVersionManifest":
        safe_version = _safe_component(version_id, name="version id")
        safe_author = _safe_component(author, name="author")
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", base_main_sha):
            raise SkillVersionError("base main SHA must be a full Git commit SHA")
        if not description.strip() or len(description.strip()) > 500:
            raise SkillVersionError("version description is required and must be concise")
        validate_artifact_set(files)
        complete_method = bool(method_name or status_badge or aliases)
        if complete_method:
            _validate_method_metadata(
                version_id=safe_version,
                method_name=method_name,
                status_badge=status_badge,
                aliases=aliases,
                paths=set(files),
            )
        domains = tuple(sorted({_artifact_domain(path) for path in files}))
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
            schema_version=2 if complete_method else 1,
            description=description.strip(),
            method_name=method_name.strip(),
            status_badge=status_badge.strip(),
            aliases=tuple(aliases),
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SkillVersionManifest":
        common = {
            "version_id", "author", "base_main_sha", "changed_domains", "created_at",
            "skill_hashes", "schema_version", "description",
        }
        try:
            schema_version = int(value["schema_version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SkillVersionError("unsupported Shadow manifest schema") from exc
        required = common if schema_version == 1 else common | {
            "method_name", "status_badge", "aliases",
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
            schema_version=schema_version,
            description=str(value["description"]),
            method_name=str(value.get("method_name", "")),
            status_badge=str(value.get("status_badge", "")),
            aliases=tuple(str(item) for item in value.get("aliases", [])),
        )
        if manifest.schema_version not in {1, 2}:
            raise SkillVersionError("unsupported Shadow manifest schema")
        if manifest.version_id != _safe_component(manifest.version_id, name="version id"):
            raise SkillVersionError("Shadow manifest has an invalid version id")
        if manifest.author != _safe_component(manifest.author, name="author"):
            raise SkillVersionError("Shadow manifest has an invalid author")
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", manifest.base_main_sha):
            raise SkillVersionError("Shadow manifest has an invalid base commit")
        for path, digest in manifest.skill_hashes.items():
            if path not in ALLOWED_SKILL_RELATIVE_PATHS or not re.fullmatch(
                r"[0-9a-f]{64}", digest
            ):
                raise SkillVersionError("Shadow manifest has an invalid Skill hash")
        expected_domains = tuple(sorted({
            _artifact_domain(path) for path in manifest.skill_hashes
        }))
        if manifest.changed_domains != expected_domains:
            raise SkillVersionError("Shadow manifest domains do not match its Skill files")
        if not manifest.description.strip() or len(manifest.description.strip()) > 500:
            raise SkillVersionError("Shadow manifest description is invalid")
        if manifest.schema_version == 2:
            _validate_method_metadata(
                version_id=manifest.version_id,
                method_name=manifest.method_name,
                status_badge=manifest.status_badge,
                aliases=manifest.aliases,
                paths=set(manifest.skill_hashes),
            )
        return manifest

    def as_dict(self) -> dict[str, Any]:
        value = {
            "version_id": self.version_id,
            "author": self.author,
            "base_main_sha": self.base_main_sha,
            "changed_domains": list(self.changed_domains),
            "created_at": self.created_at,
            "skill_hashes": dict(sorted(self.skill_hashes.items())),
            "schema_version": self.schema_version,
            "description": self.description,
        }
        if self.schema_version == 2:
            value.update({
                "method_name": self.method_name,
                "status_badge": self.status_badge,
                "aliases": list(self.aliases),
            })
        return value

    def identity(self) -> str:
        return sha256_payload(self.as_dict())


def _validate_method_metadata(
    *, version_id: str, method_name: str, status_badge: str,
    aliases: tuple[str, ...], paths: set[str],
) -> None:
    if not method_name.strip() or len(method_name.strip()) > 100:
        raise SkillVersionError("complete method name is invalid")
    if not status_badge.strip() or len(status_badge.strip()) > 40:
        raise SkillVersionError("complete method status badge is invalid")
    if paths != COMPLETE_METHOD_PATHS:
        raise SkillVersionError("complete method must embed every governed artifact")
    if len(aliases) != len(set(aliases)) or version_id in aliases:
        raise SkillVersionError("complete method aliases are invalid")
    for alias in aliases:
        if not alias or alias != alias.strip() or not re.fullmatch(r"[A-Za-z0-9._-]+", alias):
            raise SkillVersionError("complete method aliases are invalid")


@dataclass(frozen=True)
class GitHubRepositoryConfig:
    owner: str
    repository: str
    client_id: str
    branch_prefix: str = "shadow/"
    catalog_branch: str = "versions"
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
                catalog_branch=str(value.get("catalog_branch", "versions")),
                skill_package_root=str(value.get("skill_package_root", "shadow_versions")),
                token_refresh_leeway_seconds=int(
                    value.get("token_refresh_leeway_seconds", 300)
                ),
                api_base_url=str(value.get("api_base_url", "https://api.github.com")),
                web_base_url=str(value.get("web_base_url", "https://github.com")),
            )
        except (TypeError, ValueError) as exc:
            raise SkillVersionError("GitHub repository configuration is invalid") from exc
        _validate_repository_config(config)
        return config

    def personal_branch(self, login: str) -> str:
        return self.branch_prefix + _safe_component(login, name="GitHub login")


def _valid_branch_name(value: str) -> bool:
    """Validate the safe subset of GitHub branch refs accepted by the Lab."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value):
        return False
    if not value or value != value.strip() or value.startswith(("/", ".")):
        return False
    if value.endswith(("/", ".", ".lock")) or ".." in value or "@{" in value:
        return False
    if value.startswith("refs/") or any(character in value for character in " ~^:?*[\\"):
        return False
    return all(part not in {"", ".", ".."} for part in value.split("/"))


def _validate_repository_config(config: GitHubRepositoryConfig) -> None:
    if not config.owner or not config.repository:
        raise SkillVersionError("team repository is not configured")
    if not config.branch_prefix.endswith("/"):
        raise SkillVersionError("personal branch prefix must end with a slash")
    if not _valid_branch_name(config.branch_prefix + "member"):
        raise SkillVersionError("personal branch prefix is invalid")
    if not _valid_branch_name(config.catalog_branch) or config.catalog_branch.lower() in {
        "main", "master"
    }:
        raise SkillVersionError("catalog branch is invalid or protected")
    if config.token_refresh_leeway_seconds <= 0:
        raise SkillVersionError("GitHub token refresh leeway must be positive")
    if config.skill_package_root != "shadow_versions":
        raise SkillVersionError("Skill package root is invalid")


class GitHubSkillClient:
    """Least-privilege REST client; no local Git executable is required."""

    def __init__(self, *, config: GitHubRepositoryConfig, token: str = "") -> None:
        _validate_repository_config(config)
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
            error = SkillVersionError(
                f"GitHub request failed: {type(exc).__name__}: {exc}"
            )
            error.status_code = _http_status_code(exc)
            raise error from exc

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
            value = self._request(
                "GET", self._repo_path(f"/git/ref/heads/{quote(branch, safe='')}")
            )
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

    def ensure_catalog_branch(self) -> str:
        branch = self.config.catalog_branch
        if self.ref_sha(branch):
            return branch
        main_sha = self.ref_sha("main")
        if not main_sha:
            raise SkillVersionError("team repository main branch is unavailable")
        try:
            self._request(
                "POST", self._repo_path("/git/refs"),
                json={"ref": f"refs/heads/{branch}", "sha": main_sha},
            )
        except SkillVersionError as exc:
            # Another uploader may have created the catalog after our lookup.
            if "422" not in str(exc) or not self.ref_sha(branch):
                raise
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
        encoded = re.sub(r"[ \t\r\n\f\v]+", "", str(value.get("content", "")))
        try:
            return base64.b64decode(encoded, validate=True)
        except binascii.Error as exc:
            raise SkillVersionError("GitHub content response contains invalid base64") from exc

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
        validate_artifact_set(files)
        for path, content in files.items():
            if manifest.skill_hashes.get(path) != hashlib.sha256(
                content.encode("utf-8")
            ).hexdigest():
                raise SkillVersionError("manifest Skill hash differs from uploaded bytes")
        branch = self.config.catalog_branch
        root = PurePosixPath(self.config.skill_package_root) / manifest.author / manifest.version_id
        if self.content_exists((root / "manifest.json").as_posix(), ref=branch):
            raise SkillVersionError(
                "this immutable Shadow version id already exists in the team catalog"
            )
        branch = self.ensure_catalog_branch()
        payload_files = {
            (root / "manifest.json").as_posix(): json.dumps(
                manifest.as_dict(), indent=2, sort_keys=True
            ) + "\n",
            **{
                (root / _stored_artifact_path(path)).as_posix(): content
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
        for attempt in range(4):
            head = self.ref_sha(branch)
            if not head:
                branch = self.ensure_catalog_branch()
                continue
            if self.content_exists((root / "manifest.json").as_posix(), ref=head):
                raise SkillVersionError(
                    "this immutable Shadow version id already exists in the team catalog"
                )
            commit = self._request("GET", self._repo_path(f"/git/commits/{head}"))
            base_tree = str(commit.get("tree", {}).get("sha", ""))
            if not base_tree:
                raise SkillVersionError("catalog branch head has no tree")
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
            try:
                self._request(
                    "PATCH", self._repo_path(
                        f"/git/refs/heads/{quote(branch, safe='')}"
                    ),
                    json={"sha": new_commit["sha"], "force": False},
                )
            except SkillVersionError as exc:
                if attempt == 3 or not any(code in str(exc) for code in ("409", "422")):
                    raise
                continue
            return {
                "branch": branch, "commit_sha": new_commit["sha"],
                "manifest": manifest.as_dict(),
            }
        raise SkillVersionError("team catalog upload conflict retries were exhausted")

    def list_remote_versions(self) -> list[dict[str, Any]]:
        """Discover immutable manifests without executing remote repository content."""

        versions_by_identity: dict[str, dict[str, Any]] = {}
        root = self.config.skill_package_root
        sources: list[dict[str, str]] = []
        catalog_sha = self.ref_sha(self.config.catalog_branch)
        if catalog_sha:
            sources.append({"name": self.config.catalog_branch, "sha": catalog_sha})
        sources.extend(self.list_shadow_branches())
        for branch_row in sources:
            branch = branch_row["name"]
            commit_sha = branch_row["sha"]
            shared = branch == self.config.catalog_branch
            branch_author = None if shared else _safe_component(
                branch.removeprefix(self.config.branch_prefix), name="branch author"
            )
            try:
                author_rows = self.list_directory(root, ref=commit_sha)
            except SkillVersionError as exc:
                # A personal branch is created from main before its first upload,
                # so a missing package root is a valid empty catalog. The shared
                # catalog and all non-404 provider failures remain fail-closed.
                if shared or _http_status_code(exc) != 404:
                    raise
                continue
            for author_row in author_rows:
                if author_row.get("type") != "dir":
                    continue
                raw_author = str(author_row["name"])
                try:
                    author = _safe_component(raw_author, name="author")
                except SkillVersionError:
                    continue
                if author != raw_author:
                    continue
                if branch_author is not None and author != branch_author:
                    continue
                author_path = f"{root}/{author}"
                for version_row in self.list_directory(author_path, ref=commit_sha):
                    if version_row.get("type") != "dir":
                        continue
                    raw_version = str(version_row["name"])
                    try:
                        version_id = _safe_component(raw_version, name="version id")
                    except SkillVersionError:
                        continue
                    if version_id != raw_version:
                        continue
                    manifest_path = f"{author_path}/{version_id}/manifest.json"
                    try:
                        manifest = SkillVersionManifest.from_dict(json.loads(
                            self.get_content(manifest_path, ref=commit_sha).decode("utf-8")
                        ))
                    except (UnicodeDecodeError, json.JSONDecodeError, SkillVersionError):
                        continue
                    if manifest.author != author or manifest.version_id != version_id:
                        continue
                    row = {
                        **manifest.as_dict(),
                        "branch": branch,
                        "commit_sha": commit_sha,
                        "manifest_sha256": manifest.identity(),
                    }
                    versions_by_identity.setdefault(manifest.identity(), row)
        return sorted(
            versions_by_identity.values(),
            key=lambda row: (
                str(row["created_at"]), str(row["author"]), str(row["version_id"])
            ),
            reverse=True,
        )

    def download_version(
        self, *, branch: str, author: str, version_id: str
    ) -> tuple[SkillVersionManifest, dict[str, str], str]:
        safe_author = _safe_component(author, name="author")
        safe_version = _safe_component(version_id, name="version id")
        expected_branch = self.config.personal_branch(safe_author)
        if branch not in {self.config.catalog_branch, expected_branch}:
            raise SkillVersionError("Shadow package source branch is not allowed")
        commit_sha = self.ref_sha(branch)
        if not commit_sha:
            raise SkillVersionError("remote Shadow source branch is unavailable")
        root = PurePosixPath(self.config.skill_package_root) / safe_author / safe_version
        try:
            manifest = SkillVersionManifest.from_dict(json.loads(
                self.get_content((root / "manifest.json").as_posix(), ref=commit_sha).decode("utf-8")
            ))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SkillVersionError("remote Shadow manifest is invalid JSON") from exc
        if manifest.author != safe_author or manifest.version_id != safe_version:
            raise SkillVersionError("remote Shadow identity differs from its path")
        files: dict[str, str] = {}
        for relative_path, expected_hash in sorted(manifest.skill_hashes.items()):
            remote = root / _stored_artifact_path(relative_path)
            try:
                content = self.get_content(remote.as_posix(), ref=commit_sha).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SkillVersionError("remote Skill is not UTF-8 text") from exc
            validate_skill_text(relative_path, content)
            if hashlib.sha256(content.encode("utf-8")).hexdigest() != expected_hash:
                raise SkillVersionError("remote Skill bytes differ from their manifest")
            files[relative_path] = content
        return manifest, files, commit_sha


class LocalSkillRegistry:
    def __init__(self, *, root: Path, package_skills: Path) -> None:
        self.root = root.resolve()
        self.package_skills = package_skills.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def main_version(self) -> dict[str, Any]:
        hashes = {
            path.relative_to(self.package_skills.parent).as_posix(): sha256_file(path)
            for pattern in ("*/SKILL.md", "*/references/foundations.md", "*/agents/openai.yaml")
            for path in sorted(self.package_skills.glob(pattern))
        }
        decision_root = self.package_skills.parent / "decision"
        for name in ("decision.js", "cases.json"):
            path = decision_root / name
            if path.is_file():
                hashes[f"decision/{name}"] = sha256_file(path)
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
        if not re.fullmatch(r"(?:[0-9a-f]{40}|local:[0-9a-f]{64})", commit_sha):
            raise SkillVersionError("installed version requires a full Git commit SHA")
        if set(files) != set(manifest.skill_hashes):
            raise SkillVersionError("downloaded files differ from their manifest")
        validate_artifact_set(files)
        for relative_path, content in files.items():
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
                destination = temporary / _stored_artifact_path(relative_path)
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
                    key: item for key, item in value.items()
                    if key not in {"manifest_sha256", "source_commit_sha"}
                })
                if value.get("manifest_sha256") != manifest.identity():
                    continue
                versions.append({
                    **manifest.as_dict(),
                    "label": manifest.method_name or f"{manifest.author} · {manifest.description}",
                    "version_sha256": manifest.identity(),
                    "source": "local" if str(value.get("source_commit_sha", "")).startswith("local:") else "github-shadow",
                    "source_commit_sha": value.get("source_commit_sha"),
                })
            except Exception:
                continue
        return versions

    def list_method_versions(self) -> list[dict[str, Any]]:
        """Return shipped complete methods; legacy ids remain in ``list_versions``."""

        return sorted([
            row for row in self.list_versions()
            if row.get("schema_version") == 2 and row.get("method_name")
        ], key=lambda row: str(row["method_name"]))

    def selectable_versions(self) -> list[dict[str, Any]]:
        """Prefer canonical methods while retaining personal installed versions."""

        versions = self.list_versions()
        methods = [
            row for row in versions
            if row.get("schema_version") == 2 and row.get("method_name")
        ]
        legacy_aliases = {
            (str(row["author"]), alias)
            for row in methods for alias in row.get("aliases", [])
        }
        personal = [
            row for row in versions
            if not row.get("method_name")
            and (str(row.get("author", "team")), str(row["version_id"])) not in legacy_aliases
        ]
        return sorted(methods, key=lambda row: str(row["method_name"])) + personal

    def compare_methods(
        self, left_version_id: str, right_version_id: str, *, author: str
    ) -> dict[str, Any]:
        from .decision_sandbox import decision_mode

        left = self.effective_skills(left_version_id, author)
        right = self.effective_skills(right_version_id, author)
        changed = sorted(
            path for path in set(left) | set(right) if left.get(path) != right.get(path)
        )
        return {
            "left": f"{author}/{left_version_id}",
            "right": f"{author}/{right_version_id}",
            "changed_paths": changed,
            "changed_file_count": len(changed),
            "decision_mode": {
                "left": decision_mode(left["decision/cases.json"]),
                "right": decision_mode(right["decision/cases.json"]),
            },
        }

    def effective_skills(self, version_id: str, author: str = "") -> dict[str, str]:
        output = {
            path.relative_to(self.package_skills.parent).as_posix(): path.read_text(encoding="utf-8")
            for pattern in ("*/SKILL.md", "*/references/foundations.md", "*/agents/openai.yaml")
            for path in sorted(self.package_skills.glob(pattern))
        }
        decision_root = self.package_skills.parent / "decision"
        for name in ("decision.js", "cases.json"):
            path = decision_root / name
            if path.is_file():
                output[f"decision/{name}"] = path.read_text(encoding="utf-8")
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
            key: item for key, item in value.items()
            if key not in {"manifest_sha256", "source_commit_sha"}
        })
        if value.get("manifest_sha256") != manifest.identity():
            raise SkillVersionError("installed Skill manifest hash mismatch")
        for relative_path, expected in manifest.skill_hashes.items():
            source = target / _stored_artifact_path(relative_path)
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
        # A draft can accumulate edits across domains.  Replace only paths the
        # caller has explicitly supplied; preserve previously saved packages.
        existing_files: dict[str, str] = {}
        if root.exists():
            _, existing_files = self.load_draft(author=safe_author, draft_id=safe_draft)
        merged_files = {**existing_files, **files}
        staging = Path(tempfile.mkdtemp(prefix=f".{safe_draft}-", dir=root.parent))
        backup = root.with_name(f".{safe_draft}.previous")
        try:
            hashes = {}
            validate_artifact_set(merged_files)
            for path, content in merged_files.items():
                destination = staging / _draft_artifact_path(path)
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
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", str(metadata["base_main_sha"])):
            raise SkillVersionError("local Shadow draft main commit is invalid")
        files: dict[str, str] = {}
        for relative_path, expected in metadata["skill_hashes"].items():
            if relative_path not in ALLOWED_SKILL_RELATIVE_PATHS:
                raise SkillVersionError("local Shadow draft contains a disallowed path")
            source = root / _draft_artifact_path(relative_path)
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
