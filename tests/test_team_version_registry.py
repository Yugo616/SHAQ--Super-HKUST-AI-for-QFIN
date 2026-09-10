from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from shaq_daily_oracle.app_paths import AppPaths
from shaq_daily_oracle.lab_service import LabService
from shaq_daily_oracle.research_settings import ResearchSettingsStore
from shaq_daily_oracle.skill_versions import (
    GitHubRepositoryConfig,
    GitHubSkillClient,
    SkillVersionError,
    SkillVersionManifest,
    _stored_artifact_path,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def skill_text(name: str = "market-common-shock", suffix: str = "") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        "description: A concise governed research domain.\n"
        "---\n\n"
        "# Domain\n\nUse frozen evidence only.\n"
        + suffix
    )


def manifest(author: str, version_id: str, suffix: str = ""):
    files = {"skills/market-common-shock/SKILL.md": skill_text(suffix=suffix)}
    return SkillVersionManifest.create(
        author=author,
        version_id=version_id,
        base_main_sha="a" * 40,
        files=files,
        description="A governed team version.",
        created_at="2026-09-09T00:00:00+00:00",
    ), files


class FakeGitHub:
    """In-memory GitHub Git/Contents API; production logic stays real."""

    def __init__(self) -> None:
        self.serial = 0
        self.blobs: dict[str, bytes] = {}
        self.trees: dict[str, dict[str, bytes]] = {"tree-main": {}}
        self.commits = {"main-sha": {"tree": "tree-main", "parents": []}}
        self.refs = {"main": "main-sha"}
        self.before_patch = None

    def seed(self, branch: str, package: SkillVersionManifest, files: dict[str, str]) -> str:
        root = Path("shadow_versions") / package.author / package.version_id
        current = self.refs.get(branch)
        tree = dict(self.trees[self.commits[current]["tree"]]) if current else {}
        tree.update({
            (root / "manifest.json").as_posix(): (
                json.dumps(package.as_dict(), sort_keys=True) + "\n"
            ).encode(),
        })
        for relative, content in files.items():
            tree[(root / _stored_artifact_path(relative)).as_posix()] = content.encode()
        self.serial += 1
        tree_sha = f"tree-{self.serial}"
        commit_sha = f"commit-{self.serial}"
        self.trees[tree_sha] = tree
        self.commits[commit_sha] = {"tree": tree_sha, "parents": ["main-sha"]}
        self.refs[branch] = commit_sha
        return commit_sha

    def request(self, method: str, path: str, **kwargs):
        suffix = path.split("/repos/team/repo", 1)[-1]
        if method == "GET" and suffix.startswith("/git/ref/heads/"):
            from urllib.parse import unquote
            branch = unquote(suffix.removeprefix("/git/ref/heads/"))
            if branch not in self.refs:
                error = SkillVersionError("GitHub request failed: 404")
                error.status_code = 404
                raise error
            return {"object": {"sha": self.refs[branch]}}
        if method == "POST" and suffix == "/git/refs":
            branch = kwargs["json"]["ref"].removeprefix("refs/heads/")
            if branch in self.refs:
                raise SkillVersionError("GitHub request failed: 422 reference exists")
            self.refs[branch] = kwargs["json"]["sha"]
            return {"ref": kwargs["json"]["ref"]}
        if method == "GET" and suffix == "/branches?per_page=100":
            return [
                {"name": name, "commit": {"sha": sha}}
                for name, sha in sorted(self.refs.items())
            ]
        if method == "GET" and suffix.startswith("/git/commits/"):
            sha = suffix.removeprefix("/git/commits/")
            row = self.commits[sha]
            return {"tree": {"sha": row["tree"]}}
        if method == "POST" and suffix == "/git/blobs":
            content = kwargs["json"]["content"].encode()
            sha = hashlib.sha256(content).hexdigest()
            self.blobs[sha] = content
            return {"sha": sha}
        if method == "POST" and suffix == "/git/trees":
            base = dict(self.trees[kwargs["json"]["base_tree"]])
            for row in kwargs["json"]["tree"]:
                base[row["path"]] = self.blobs[row["sha"]]
            self.serial += 1
            sha = f"tree-{self.serial}"
            self.trees[sha] = base
            return {"sha": sha}
        if method == "POST" and suffix == "/git/commits":
            self.serial += 1
            sha = f"commit-{self.serial}"
            self.commits[sha] = {
                "tree": kwargs["json"]["tree"],
                "parents": kwargs["json"]["parents"],
            }
            return {"sha": sha}
        if method == "PATCH" and suffix.startswith("/git/refs/heads/"):
            from urllib.parse import unquote
            branch = unquote(suffix.removeprefix("/git/refs/heads/"))
            new_sha = kwargs["json"]["sha"]
            if self.before_patch:
                callback, self.before_patch = self.before_patch, None
                callback()
            expected_parent = self.commits[new_sha]["parents"][0]
            if self.refs.get(branch) != expected_parent:
                raise SkillVersionError("GitHub request failed: 422 non-fast-forward")
            self.refs[branch] = new_sha
            return {"object": {"sha": new_sha}}
        if method == "GET" and suffix.startswith("/contents/"):
            location, ref = suffix.removeprefix("/contents/").split("?ref=", 1)
            from urllib.parse import unquote

            location, ref = unquote(location), unquote(ref)
            commit = self.refs.get(ref, ref)
            if commit not in self.commits:
                error = SkillVersionError("GitHub request failed: 404")
                error.status_code = 404
                raise error
            tree = self.trees[self.commits[commit]["tree"]]
            if location in tree:
                return {
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(tree[location]).decode(),
                }
            prefix = location.rstrip("/") + "/"
            children = {}
            for candidate in tree:
                if candidate.startswith(prefix):
                    tail = candidate[len(prefix):]
                    name = tail.split("/", 1)[0]
                    children[name] = "dir" if "/" in tail else "file"
            if not children:
                error = SkillVersionError("GitHub request failed: 404")
                error.status_code = 404
                raise error
            return [
                {"name": name, "path": prefix + name, "type": kind}
                for name, kind in sorted(children.items())
            ]
        raise AssertionError((method, suffix, kwargs))


class TeamVersionRegistryTests(unittest.TestCase):
    def config(self, **changes):
        values = dict(
            owner="team", repository="repo", client_id="client",
            catalog_branch="versions",
        )
        values.update(changes)
        return GitHubRepositoryConfig(**values)

    def client(self, remote: FakeGitHub) -> GitHubSkillClient:
        client = GitHubSkillClient(config=self.config(), token="token")
        client._request = remote.request  # type: ignore[method-assign]
        return client

    def test_shared_discovery_lists_multiple_authors_and_prefers_shared_duplicate(self):
        remote = FakeGitHub()
        alice, alice_files = manifest("alice", "market-1")
        bob, bob_files = manifest("bob", "market-2", "Bob.\n")
        shared_sha = remote.seed("versions", alice, alice_files)
        remote.seed("versions", bob, bob_files)
        shared_sha = remote.refs["versions"]
        remote.seed("shadow/alice", alice, alice_files)

        rows = self.client(remote).list_remote_versions()

        self.assertEqual([(r["author"], r["version_id"]) for r in rows], [
            ("bob", "market-2"), ("alice", "market-1")
        ])
        alice_row = next(r for r in rows if r["author"] == "alice")
        self.assertEqual(alice_row["branch"], "versions")
        self.assertEqual(alice_row["commit_sha"], shared_sha)

    def test_legacy_personal_branch_remains_readable(self):
        remote = FakeGitHub()
        package, files = manifest("alice", "legacy-1")
        commit = remote.seed("shadow/alice", package, files)
        rows = self.client(remote).list_remote_versions()
        self.assertEqual(rows[0]["branch"], "shadow/alice")
        self.assertEqual(rows[0]["commit_sha"], commit)

    def test_empty_personal_branch_does_not_hide_shared_catalog_versions(self):
        remote = FakeGitHub()
        package, files = manifest("team", "baseline-1")
        remote.seed("versions", package, files)
        # A valid personal branch can exist before it contains a package root.
        remote.refs["shadow/alice"] = remote.refs["main"]

        rows = self.client(remote).list_remote_versions()

        self.assertEqual(
            [(row["author"], row["version_id"], row["branch"]) for row in rows],
            [("team", "baseline-1", "versions")],
        )

    def test_personal_branch_directory_auth_failure_is_not_treated_as_empty(self):
        remote = FakeGitHub()
        remote.refs["shadow/alice"] = remote.refs["main"]
        original_request = remote.request

        def forbidden(method: str, path: str, **kwargs):
            if method == "GET" and "/contents/shadow_versions?ref=main-sha" in path:
                error = SkillVersionError(
                    "GitHub request failed: 403 for /contents/shadow_versions?ref=sha404"
                )
                error.status_code = 403
                raise error
            return original_request(method, path, **kwargs)

        client = self.client(remote)
        client._request = forbidden  # type: ignore[method-assign]

        with self.assertRaisesRegex(SkillVersionError, "403"):
            client.list_remote_versions()

    def test_upload_uses_catalog_without_mutating_main_and_rejects_duplicate_id(self):
        remote = FakeGitHub()
        package, files = manifest("alice", "market-1")
        result = self.client(remote).upload_version(login="Alice", manifest=package, files=files)
        self.assertEqual(result["branch"], "versions")
        self.assertEqual(remote.refs["main"], "main-sha")
        with self.assertRaisesRegex(SkillVersionError, "already exists"):
            self.client(remote).upload_version(login="alice", manifest=package, files=files)

    def test_concurrent_upload_retry_preserves_both_versions(self):
        remote = FakeGitHub()
        alice, alice_files = manifest("alice", "market-1")
        bob, bob_files = manifest("bob", "market-2", "Bob.\n")
        alice_client = self.client(remote)
        bob_client = self.client(remote)
        remote.before_patch = lambda: bob_client.upload_version(
            login="bob", manifest=bob, files=bob_files
        )
        alice_client.upload_version(login="alice", manifest=alice, files=alice_files)
        rows = alice_client.list_remote_versions()
        self.assertEqual({(r["author"], r["version_id"]) for r in rows}, {
            ("alice", "market-1"), ("bob", "market-2")
        })

    def test_download_pins_one_commit_and_rejects_tampered_package(self):
        remote = FakeGitHub()
        package, files = manifest("alice", "market-1")
        pinned = remote.seed("versions", package, files)
        client = self.client(remote)
        original_get = client.get_content

        def advance_after_manifest(path: str, *, ref: str):
            value = original_get(path, ref=ref)
            if path.endswith("manifest.json"):
                newer, newer_files = manifest("bob", "other", "newer\n")
                remote.seed("versions", newer, newer_files)
            return value

        client.get_content = advance_after_manifest  # type: ignore[method-assign]
        downloaded, downloaded_files, commit = client.download_version(
            branch="versions", author="alice", version_id="market-1"
        )
        self.assertEqual(commit, pinned)
        self.assertEqual(downloaded, package)
        self.assertEqual(downloaded_files, files)

        path = "shadow_versions/alice/market-1/changed_skills/market-common-shock/SKILL.md"
        current = remote.refs["versions"]
        tree = remote.trees[remote.commits[current]["tree"]]
        tree[path] += b"tampered"
        with self.assertRaisesRegex(SkillVersionError, "differ"):
            self.client(remote).download_version(
                branch="versions", author="alice", version_id="market-1"
            )

    def test_complete_method_upload_download_preserves_every_embedded_byte(self):
        package = json.loads(
            (PACKAGE_ROOT / "bundled_versions/independent-gate.json").read_text(
                encoding="utf-8"
            )
        )
        manifest = SkillVersionManifest.from_dict(package["manifest"])
        remote = FakeGitHub()
        client = self.client(remote)

        uploaded = client.upload_version(
            login="team", manifest=manifest, files=package["files"]
        )
        downloaded, files, commit = client.download_version(
            branch="versions", author="team", version_id="independent-gate-1"
        )

        self.assertEqual(remote.refs["main"], "main-sha")
        self.assertEqual(commit, uploaded["commit_sha"])
        self.assertEqual(downloaded.as_dict(), package["manifest"])
        self.assertEqual(files, package["files"])
        self.assertEqual(downloaded.method_name, "独立证据门禁版")
        self.assertEqual(downloaded.status_badge, "正式基准")

    def test_invalid_catalog_branch_and_package_root_are_rejected(self):
        for value in (
            "main", "master", "bad branch", "refs/heads/versions", "a..b",
            "main#catalog", "versions%23escape", "versions\nnext",
        ):
            with self.subTest(value=value), self.assertRaises(SkillVersionError):
                GitHubRepositoryConfig.from_dict({
                    "owner": "team", "repository": "repo", "catalog_branch": value,
                })

    def test_client_rejects_unsafe_direct_dataclass_config(self):
        for value in ("main", "main#catalog", "versions%23escape"):
            with self.subTest(value=value), self.assertRaises(SkillVersionError):
                GitHubSkillClient(
                    config=GitHubRepositoryConfig(
                        owner="team", repository="repo", client_id="client",
                        catalog_branch=value,
                    )
                )

    def test_ref_endpoint_percent_encodes_branch_as_one_url_component(self):
        captured = {}

        class Response:
            content = b"{}"

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"object": {"sha": "a" * 40}}

        class Httpx:
            @staticmethod
            def request(method, url, **kwargs):
                captured["url"] = url
                return Response()

        client = GitHubSkillClient(config=self.config(catalog_branch="release/catalog"))
        with patch.object(client, "_httpx", return_value=Httpx):
            self.assertEqual(client.ref_sha("release/catalog"), "a" * 40)
        parsed = urlsplit(captured["url"])
        self.assertEqual(parsed.fragment, "")
        self.assertTrue(parsed.path.endswith("/git/ref/heads/release%2Fcatalog"))
        for value in ("../shadow_versions", "/shadow_versions", "other"):
            with self.subTest(value=value), self.assertRaises(SkillVersionError):
                GitHubRepositoryConfig.from_dict({
                    "owner": "team", "repository": "repo",
                    "skill_package_root": value,
                })

    def test_github_content_accepts_standard_lf_and_crlf_wrapped_base64(self):
        payload = b"A public README payload long enough to wrap across several lines."
        encoded = base64.b64encode(payload).decode("ascii")
        client = self.client(FakeGitHub())
        for separator in ("\n", "\r\n"):
            wrapped = separator.join(
                encoded[index:index + 12] for index in range(0, len(encoded), 12)
            )
            with self.subTest(separator=repr(separator)), patch.object(
                client, "_request",
                return_value={"type": "file", "encoding": "base64", "content": wrapped},
            ):
                self.assertEqual(client.get_content("README.md", ref="main"), payload)

    def test_github_content_rejects_non_whitespace_invalid_base64_as_domain_error(self):
        client = self.client(FakeGitHub())
        with patch.object(
            client, "_request",
            return_value={"type": "file", "encoding": "base64", "content": "YWJj$A=="},
        ), self.assertRaisesRegex(SkillVersionError, "base64"):
            client.get_content("README.md", ref="main")

    def test_old_settings_migrate_to_versions_without_touching_real_settings(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            settings_path = root / "research-settings.json"
            settings_path.write_text(json.dumps({
                "github": {"owner": "old", "repository": "repo", "branch_prefix": "shadow/"}
            }))
            paths = AppPaths(
                package_root=PACKAGE_ROOT, data_root=root / "data", config_root=root,
                log_root=root / "logs", runtime_root=root / "runtime",
                dashboard_db=root / "db", settings_file=root / "settings.json",
                effective_ai_config=root / "ai.json", research_root=root / "research",
                batches_root=root / "batches", skill_registry_root=root / "registry",
                research_database=root / "research.sqlite3",
                research_settings_file=settings_path,
            ).ensure()
            loaded = ResearchSettingsStore(paths).load()
        self.assertEqual(loaded["github"]["catalog_branch"], "versions")

    def test_github_login_does_not_create_personal_branch(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            paths = AppPaths(
                package_root=PACKAGE_ROOT, data_root=root / "data", config_root=root,
                log_root=root / "logs", runtime_root=root / "runtime",
                dashboard_db=root / "db", settings_file=root / "settings.json",
                effective_ai_config=root / "ai.json", research_root=root / "research",
                batches_root=root / "batches", skill_registry_root=root / "registry",
                research_database=root / "research.sqlite3",
                research_settings_file=root / "research-settings.json",
            ).ensure()
            with patch("shaq_daily_oracle.bundled_versions.install_bundled_versions"):
                service = LabService(paths)
            fake = unittest.mock.Mock()
            fake.config = self.config()
            fake.poll_device_flow.return_value = {"status": "authorized", "token": "token"}
            authorized = unittest.mock.Mock()
            authorized.config = self.config()
            authorized.authenticated_user.return_value = {"login": "alice"}
            authorized.repository_access.return_value = {"read": True, "write": True}
            with patch.object(service, "_github_client", return_value=fake), patch(
                "shaq_daily_oracle.lab_service.GitHubSkillClient", return_value=authorized
            ), patch.object(service.settings, "save_github_session"):
                result = service.complete_github_login("device")
        self.assertEqual(result["catalog_branch"], "versions")
        authorized.ensure_personal_branch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
