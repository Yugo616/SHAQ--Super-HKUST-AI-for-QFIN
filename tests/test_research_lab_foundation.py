from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
import sys
import httpx
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from shaq_daily_oracle.app_paths import AppPaths
from shaq_daily_oracle.data_providers import (
    DataProfile,
    DataProviderError,
    OpenBBRestProvider,
    load_versioned_universe,
    provider_manifest,
)
from shaq_daily_oracle.model_backends import (
    ModelBackendError,
    ModelProfile,
    _http_post_json,
    call_structured,
    uses_local_subscription,
)
from shaq_daily_oracle.lab_service import LabService, LabServiceError
from shaq_daily_oracle.research_settings import ResearchSettingsStore
from shaq_daily_oracle.skill_versions import (
    GitHubRepositoryConfig,
    GitHubSkillClient,
    LocalSkillRegistry,
    SkillVersionError,
    SkillVersionManifest,
    validate_skill_text,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def skill_text(name: str = "market-common-shock") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        "description: A concise governed research domain.\n"
        "---\n\n"
        "# Domain\n\nUse only frozen evidence and abstain when evidence is insufficient.\n"
    )


class ResearchLabFoundationTests(unittest.TestCase):
    def test_restart_preserves_account_rules_and_activation_time(self):
        from shaq_daily_oracle.virtual_accounts import AccountStore, AccountRules
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.paths(Path(tmp))
            store = AccountStore(paths.research_root / 'virtual_accounts')
            policy = store.activate(AccountRules(commission_rate=0), '2026-09-09T07:00:00-04:00')
            LabService(paths)
            self.assertEqual(json.loads((store.root / 'activation.json').read_text()), policy)

    def test_offline_copy_and_save_preserve_complete_skill_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            lab = LabService(self.paths(Path(tmp)))
            with patch('keyring.get_password', side_effect=AssertionError('unexpected keychain access')):
                copy = lab.copy_local_version(version_id='main', author='team')
                saved = lab.finalize_local_version(draft_id=copy['draft_id'], description='local copy')
                actual = lab.registry.effective_skills(saved['version_id'], saved['author'])
            self.assertEqual(actual, lab.registry.effective_skills('main', 'team'))

    def test_github_username_case_does_not_hide_saved_local_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            lab = LabService(self.paths(Path(tmp)))
            with patch.object(lab.settings, 'load', return_value={'github_login':'TeamMember'}):
                copy = lab.copy_local_version(version_id='main', author='team')
                saved = lab.finalize_local_version(draft_id=copy['draft_id'], description='case test')
            self.assertEqual(len(lab._resolve_variants([saved])),1)

    def paths(self, root: Path) -> AppPaths:
        return AppPaths(
            package_root=PACKAGE_ROOT,
            data_root=root / "data",
            config_root=root / "config",
            log_root=root / "logs",
            runtime_root=root / "runtime",
            dashboard_db=root / "dashboard.sqlite3",
            settings_file=root / "settings.json",
            effective_ai_config=root / "ai-backend.json",
            research_root=root / "research",
            batches_root=root / "research/batches",
            skill_registry_root=root / "research/skills",
            research_database=root / "research/research.sqlite3",
            research_settings_file=root / "config/research-settings.json",
        ).ensure()

    def test_model_profile_rejects_insecure_or_unknown_endpoint(self) -> None:
        with self.assertRaises(ModelBackendError):
            ModelProfile.from_dict({
                "profile_id": "relay", "protocol": "openai-chat-completions",
                "base_url": "http://relay.invalid/v1", "model": "model",
            })
        with self.assertRaises(ModelBackendError):
            ModelProfile.from_dict({
                "profile_id": "relay", "protocol": "other",
                "base_url": "https://relay.invalid/v1", "model": "model",
            })

    def test_local_subscription_profile_needs_no_api_key(self) -> None:
        profile = ModelProfile.from_dict({
            "profile_id": "codex", "protocol": "codex-cli", "base_url": "",
            "model": "subscription-default",
        })
        self.assertTrue(uses_local_subscription(profile))
        self.assertNotEqual(profile.endpoint_fingerprint(), ModelProfile.from_dict({
            "profile_id": "claude", "protocol": "claude-code", "base_url": "",
            "model": "subscription-default",
        }).endpoint_fingerprint())

    def test_openai_responses_disables_storage_and_tools(self) -> None:
        captured = {}

        class Response:
            id = "resp-1"
            model = "gpt-test"
            output_text = '{"status":"ready"}'
            usage = None

        class Responses:
            def create(self, **kwargs):
                captured.update(kwargs)
                return Response()

        class Client:
            def __init__(self, **kwargs):
                captured["client"] = kwargs
                self.responses = Responses()

        profile = ModelProfile(
            profile_id="openai", protocol="openai-responses",
            base_url="https://api.openai.com/v1", model="gpt-test",
        )
        schema = {
            "type": "object", "additionalProperties": False,
            "required": ["status"], "properties": {"status": {"const": "ready"}},
        }
        with patch("openai.OpenAI", Client):
            result, audit = call_structured(
                profile=profile, secret="secret-value", prompt="frozen", schema=schema
            )
        self.assertEqual(result, {"status": "ready"})
        self.assertFalse(captured["store"])
        self.assertEqual(captured["tools"], [])
        self.assertEqual(captured["tool_choice"], "none")
        self.assertNotIn("secret-value", json.dumps(audit))
        self.assertFalse(audit["model_facing_tools_allowed"])

    def test_compatible_relay_is_strictly_validated_and_never_retried_elsewhere(self) -> None:
        profile = ModelProfile(
            profile_id="relay", protocol="openai-chat-completions",
            base_url="https://relay.invalid/v1", model="relay-model",
            output_mode="local_validated",
        )
        schema = {
            "type": "object", "additionalProperties": False,
            "required": ["answer"], "properties": {"answer": {"type": "string"}},
        }
        response = {
            "id": "chat-1", "model": "relay-model",
            "choices": [{"message": {"content": '{"wrong":true}'}}],
        }
        with patch(
            "shaq_daily_oracle.model_backends._http_post_json", return_value=response
        ) as request:
            with self.assertRaises(ModelBackendError):
                call_structured(
                    profile=profile, secret="relay-secret", prompt="packet", schema=schema
                )
        request.assert_called_once()

    def test_endpoint_401_429_and_timeout_fail_closed(self) -> None:
        for failure in (
            httpx.HTTPStatusError(
                "401 unauthorized", request=httpx.Request("POST", "https://relay.invalid"),
                response=httpx.Response(401),
            ),
            httpx.HTTPStatusError(
                "429 rate limited", request=httpx.Request("POST", "https://relay.invalid"),
                response=httpx.Response(429),
            ),
            httpx.ReadTimeout("timed out"),
        ):
            with self.subTest(failure=type(failure).__name__), patch(
                "httpx.post", side_effect=failure
            ):
                with self.assertRaises(ModelBackendError):
                    _http_post_json(
                        url="https://relay.invalid/v1/chat/completions",
                        headers={}, payload={}, timeout=1,
                    )

    def test_compatible_endpoint_cannot_silently_return_another_model(self) -> None:
        profile = ModelProfile(
            profile_id="relay", protocol="openai-chat-completions",
            base_url="https://relay.invalid/v1", model="expected-model",
            output_mode="local_validated",
        )
        schema = {
            "type": "object", "additionalProperties": False,
            "required": ["answer"], "properties": {"answer": {"type": "string"}},
        }
        response = {
            "id": "chat-1", "model": "cheaper-model",
            "choices": [{"message": {"content": '{"answer":"ok"}'}}],
        }
        with patch(
            "shaq_daily_oracle.model_backends._http_post_json", return_value=response
        ):
            with self.assertRaisesRegex(ModelBackendError, "different model"):
                call_structured(
                    profile=profile, secret="relay-secret", prompt="packet", schema=schema
                )

    def test_anthropic_native_schema_request_contains_no_tools(self) -> None:
        profile = ModelProfile(
            profile_id="claude", protocol="anthropic-messages",
            base_url="https://api.anthropic.com", model="claude-test",
            auth_style="x-api-key", output_mode="strict",
        )
        schema = {
            "type": "object", "additionalProperties": False,
            "required": ["status"], "properties": {"status": {"const": "ready"}},
        }
        response = {
            "id": "msg-1", "model": "claude-test",
            "content": [{"type": "text", "text": '{"status":"ready"}'}],
        }
        with patch(
            "shaq_daily_oracle.model_backends._http_post_json", return_value=response
        ) as request:
            result, audit = call_structured(
                profile=profile, secret="claude-secret", prompt="packet", schema=schema
            )
        payload = request.call_args.kwargs["payload"]
        self.assertNotIn("tools", payload)
        self.assertEqual(payload["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(result, {"status": "ready"})
        self.assertEqual(audit["schema_enforcement"], "native")

    def test_research_settings_keep_secrets_out_of_json(self) -> None:
        secrets = {}

        class Keyring:
            @staticmethod
            def set_password(service, name, value):
                secrets[(service, name)] = value

            @staticmethod
            def get_password(service, name):
                return secrets.get((service, name))

        with tempfile.TemporaryDirectory() as name:
            store = ResearchSettingsStore(self.paths(Path(name)))
            with patch.object(store, "_keyring", return_value=Keyring):
                store.save_model_profile({
                    "profile_id": "claude", "protocol": "anthropic-messages",
                    "base_url": "https://api.anthropic.com", "model": "claude-test",
                    "auth_style": "x-api-key",
                }, secret="top-secret")
                before_login = store.save_research_setup(
                    {"sec_identity": "Research contact@example.edu"}
                )
                store.save_github_session(
                    login="member",
                    token="github-secret",
                    refresh_token="github-refresh-secret",
                    expires_in=28_800,
                    refresh_token_expires_in=15_897_600,
                )
                before_readiness = store.public_settings()
                profile_hash = DataProfile.from_dict(
                    before_readiness["data_profile"]
                ).identity()
                store.save_research_readiness({
                    "status": "ready",
                    "data_profile_sha256": profile_hash,
                    "checked_at": "2026-09-07T12:00:00-04:00",
                    "universe_members": 500,
                    "free_bytes": 1_000_000,
                    "storage_writable": True,
                })
                saved = store.public_settings()
                text = store.paths.research_settings_file.read_text(encoding="utf-8")
        self.assertFalse(before_login["setup_complete"])
        self.assertFalse(before_readiness["setup_complete"])
        self.assertNotIn("top-secret", text)
        self.assertNotIn("github-secret", text)
        self.assertNotIn("github-refresh-secret", text)
        self.assertTrue(saved["model_secret_saved"]["claude"])
        self.assertTrue(saved["github_token_saved"])
        self.assertTrue(saved["github_refresh_saved"])
        self.assertTrue(saved["setup_complete"])

    def test_readiness_is_bound_to_current_data_profile_and_old_state_is_invalidated(self) -> None:
        secrets = {}

        class Keyring:
            @staticmethod
            def set_password(service, name, value):
                secrets[(service, name)] = value

            @staticmethod
            def get_password(service, name):
                return secrets.get((service, name))

        with tempfile.TemporaryDirectory() as name:
            store = ResearchSettingsStore(self.paths(Path(name)))
            with patch.object(store, "_keyring", return_value=Keyring):
                store.save_model_profile({
                    "profile_id": "openai", "protocol": "openai-responses",
                    "base_url": "https://api.openai.com/v1", "model": "gpt-test",
                }, secret="model-secret")
                store.save_research_setup(
                    {"sec_identity": "Research contact@example.edu"}
                )
                store.save_github_session(login="member", token="github-secret")
                self.assertFalse(store.load()["setup_complete"])
                profile_hash = DataProfile.from_dict(
                    store.load()["data_profile"]
                ).identity()
                ready = store.save_research_readiness({
                    "status": "ready",
                    "data_profile_sha256": profile_hash,
                    "checked_at": "2026-09-07T12:00:00-04:00",
                    "universe_members": 500,
                    "free_bytes": 1_000_000,
                    "storage_writable": True,
                })
                self.assertTrue(ready["setup_complete"])
                changed = store.save_research_setup({
                    "data_profile": {"intraday_interval": "15m"}
                })
                self.assertFalse(changed["setup_complete"])
                self.assertEqual(changed["research_readiness"]["status"], "not_checked")

                persisted = store.load()
                persisted["setup_complete"] = True
                persisted["research_readiness"] = {
                    "status": "ready",
                    "data_profile_sha256": "0" * 64,
                    "checked_at": "2026-09-07T12:00:00-04:00",
                }
                store._save(persisted)
                self.assertFalse(store.load()["setup_complete"])

    def test_environment_check_uses_bundled_pit_universe_and_writable_storage(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            service = LabService(self.paths(Path(name)))
            saved = service.check_research_environment()
        readiness = saved["research_readiness"]
        self.assertEqual(readiness["status"], "ready")
        self.assertGreater(readiness["universe_members"], 0)
        self.assertGreater(readiness["free_bytes"], 0)
        self.assertTrue(readiness["storage_writable"])
        self.assertNotIn(str(Path.home()), json.dumps(readiness))

    def test_lab_state_is_broker_free_and_lists_two_canonical_methods(self) -> None:
        class EmptyKeyring:
            @staticmethod
            def get_password(service, name):
                return None

        with tempfile.TemporaryDirectory() as name:
            paths = self.paths(Path(name))
            service = LabService(paths)
            had_futu = "futu" in sys.modules
            with patch.object(service.settings, "_keyring", return_value=EmptyKeyring):
                state = service.state()
            all_ids = {row["version_id"] for row in service.registry.list_versions()}
            old_selection = service._resolve_variants(
                [{"author": "team", "version_id": "main"}]
            )[0]
        self.assertEqual(state["product_name"], "SHAQ Daily Oracle Lab")
        self.assertFalse(state["research_mode"]["orders_allowed"])
        self.assertFalse(state["research_mode"]["broker_modules_loaded"])
        self.assertEqual(
            [(row["method_name"], row["status_badge"]) for row in state["versions"]],
            [("独立证据门禁版", "正式基准"), ("跨域综合研判版", "Shadow")],
        )
        self.assertEqual(
            all_ids,
            {"main", "synthesis-1", "independent-gate-1", "cross-domain-synthesis-1"},
        )
        self.assertEqual(
            old_selection.version_id,
            "main",
        )
        self.assertEqual("futu" in sys.modules, had_futu)

    def test_read_only_method_comparison_normalizes_legacy_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            service = LabService(self.paths(Path(name)))
            before = {
                str(path.relative_to(service.registry.root)): path.read_bytes()
                for path in service.registry.root.rglob("*") if path.is_file()
            }

            comparison = service.compare_methods(
                {"author": "team", "version_id": "main"},
                {"author": "team", "version_id": "synthesis-1"},
            )

            after = {
                str(path.relative_to(service.registry.root)): path.read_bytes()
                for path in service.registry.root.rglob("*") if path.is_file()
            }
        self.assertEqual(comparison["left_method"]["version_id"], "independent-gate-1")
        self.assertEqual(comparison["left_method"]["method_name"], "独立证据门禁版")
        self.assertEqual(comparison["left_method"]["status_badge"], "正式基准")
        self.assertEqual(comparison["right_method"]["version_id"], "cross-domain-synthesis-1")
        self.assertEqual(comparison["right_method"]["method_name"], "跨域综合研判版")
        self.assertEqual(comparison["right_method"]["status_badge"], "Shadow")
        self.assertGreater(comparison["changed_file_count"], 0)
        self.assertNotEqual(
            comparison["decision_mode"]["left"], comparison["decision_mode"]["right"]
        )
        self.assertEqual(before, after)

    def test_github_device_flow_preserves_and_rotates_expiring_credentials(self) -> None:
        class Response:
            def __init__(self, value):
                self.value = value

            def raise_for_status(self):
                return None

            def json(self):
                return self.value

        initial = {
            "access_token": "first-access",
            "expires_in": 28_800,
            "refresh_token": "first-refresh",
            "refresh_token_expires_in": 15_897_600,
            "scope": "",
        }
        rotated = {
            "access_token": "second-access",
            "expires_in": 28_800,
            "refresh_token": "second-refresh",
            "refresh_token_expires_in": 15_897_600,
            "scope": "",
        }
        client = GitHubSkillClient(
            config=GitHubRepositoryConfig(
                owner="team", repository="repo", client_id="client"
            )
        )
        with patch("httpx.post", side_effect=[Response(initial), Response(rotated)]) as post:
            authorized = client.poll_device_flow("device-code")
            refreshed = client.refresh_user_token(authorized["refresh_token"])
        self.assertEqual(authorized["token"], "first-access")
        self.assertEqual(refreshed["token"], "second-access")
        refresh_request = post.call_args_list[1].kwargs["data"]
        self.assertEqual(refresh_request["grant_type"], "refresh_token")
        self.assertNotIn("client_secret", refresh_request)

        with self.assertRaisesRegex(SkillVersionError, "incomplete"):
            client._validated_token_result({
                "access_token": "orphan-access", "expires_in": 28_800
            })
        with self.assertRaisesRegex(SkillVersionError, "incomplete"):
            client._validated_token_result({
                "access_token": "orphan-access", "refresh_token": "orphan-refresh"
            })
        with self.assertRaisesRegex(SkillVersionError, "configuration is invalid"):
            GitHubRepositoryConfig.from_dict({
                "owner": "team", "repository": "repo",
                "token_refresh_leeway_seconds": "not-a-number",
            })

    def test_lab_refreshes_expiring_github_token_before_repository_access(self) -> None:
        secrets = {}

        class Keyring:
            @staticmethod
            def set_password(service, name, value):
                secrets[(service, name)] = value

            @staticmethod
            def get_password(service, name):
                return secrets.get((service, name))

            @staticmethod
            def delete_password(service, name):
                secrets.pop((service, name), None)

        with tempfile.TemporaryDirectory() as name:
            service = LabService(self.paths(Path(name)))
            with patch.object(service.settings, "_keyring", return_value=Keyring):
                service.settings.save_research_setup({
                    "github": {"github_app_client_id": "client"}
                })
                service.settings.save_github_session(
                    login="member",
                    token="old-access",
                    upload_allowed=True,
                    refresh_token="old-refresh",
                    expires_in=1,
                    refresh_token_expires_in=15_897_600,
                )
                settings = service.settings.load()
                settings["github_token_expires_at"] = "2000-01-01T00:00:00+00:00"
                service.settings._save(settings)
                with patch.object(
                    GitHubSkillClient,
                    "refresh_user_token",
                    return_value={
                        "status": "authorized",
                        "token": "new-access",
                        "expires_in": 28_800,
                        "refresh_token": "new-refresh",
                        "refresh_token_expires_in": 15_897_600,
                        "scope": "",
                    },
                ) as refresh:
                    client = service._github_client(require_token=True)
                refreshed_settings = service.settings.load()
                self.assertEqual(service.settings.get_github_token(), "new-access")
                self.assertEqual(
                    service.settings.get_github_refresh_token(), "new-refresh"
                )
        refresh.assert_called_once_with("old-refresh")
        self.assertEqual(client.token, "new-access")
        self.assertGreater(
            datetime.fromisoformat(refreshed_settings["github_token_expires_at"]),
            datetime.now(ZoneInfo("UTC")),
        )

    def test_expired_github_session_without_complete_refresh_metadata_fails_closed(self) -> None:
        secrets = {}

        class Keyring:
            @staticmethod
            def set_password(service, name, value):
                secrets[(service, name)] = value

            @staticmethod
            def get_password(service, name):
                return secrets.get((service, name))

        with tempfile.TemporaryDirectory() as name:
            service = LabService(self.paths(Path(name)))
            with patch.object(service.settings, "_keyring", return_value=Keyring):
                service.settings.save_research_setup({
                    "github": {"github_app_client_id": "client"}
                })
                service.settings.save_github_session(
                    login="member", token="expired-access",
                    refresh_token="refresh-without-valid-metadata",
                )
                settings = service.settings.load()
                settings["github_token_expires_at"] = "2000-01-01T00:00:00+00:00"
                service.settings._save(settings)
                with self.assertRaisesRegex(LabServiceError, "重新登录"):
                    service._github_client(require_token=True)
                self.assertFalse(
                    service.settings._github_credentials_available(
                        service.settings.load()
                    )
                )

    def test_batch_estimate_reports_calls_time_and_domain_availability(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            service = LabService(self.paths(Path(name)))
            profile = ModelProfile(
                profile_id="estimate",
                protocol="openai-responses",
                base_url="https://api.openai.com/v1",
                model="test-model",
                max_concurrency=2,
                timeout_seconds=120,
            )
            settings = {
                "data_profile": {
                    "profile_id": "free-research",
                    "universe_file": "config/research-universe.csv",
                }
            }
            with patch.object(service.settings, "load", return_value=settings), patch.object(
                service.settings, "model_profile", return_value=profile
            ):
                estimate = service.estimate_batch(
                    [{"author": "team", "version_id": "main"}],
                    model_profile_id="estimate",
                )
        self.assertEqual(estimate["selected_versions"], 1)
        self.assertGreater(estimate["maximum_unique_model_calls"], 0)
        self.assertGreater(estimate["maximum_model_wait_minutes"], 0)
        self.assertIn("市场", estimate["available_domains"])
        self.assertTrue(any("资金" in row for row in estimate["unavailable_domains"]))

    def test_skill_editor_returns_a_real_diff_from_main(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            service = LabService(self.paths(Path(name)))
            main = service.skill_document(
                version_id="main", author="team", skill_name="market-common-shock"
            )
            replacement = skill_text("market-common-shock") + "\nUse a named mechanism.\n"
            files = {"skills/market-common-shock/SKILL.md": replacement}
            manifest = SkillVersionManifest.create(
                version_id="market-mechanism", author="alice",
                base_main_sha="a" * 40, files=files,
                description="Expose one reviewed market mechanism.",
            )
            service.registry.install(
                manifest=manifest, files=files, commit_sha="b" * 40
            )
            shadow = service.skill_document(
                version_id="market-mechanism", author="alice",
                skill_name="market-common-shock",
            )
        self.assertEqual(main["changed_lines"], 0)
        self.assertEqual(main["diff_from_main"], "")
        self.assertGreater(shadow["changed_lines"], 0)
        self.assertIn("Use a named mechanism.", shadow["diff_from_main"])

    def test_versioned_universe_respects_known_from_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "universe.csv"
            path.write_text(
                "instrument,company_name,gics_sector,gics_sub_industry,cik_company_id,known_from_utc\n"
                "AAA,Alpha,Tech,Software,CIK:0000000001,2026-01-01T00:00:00Z\n"
                "BBB,Beta,Health,Biotech,CIK:0000000002,2027-01-01T00:00:00Z\n",
                encoding="utf-8",
            )
            members = load_versioned_universe(
                path,
                cutoff=datetime(2026, 9, 7, tzinfo=ZoneInfo("America/New_York")),
            )
        self.assertEqual([member.symbol for member in members], ["AAA"])
        self.assertEqual(members[0].cik, "0000000001")

    def test_provider_manifest_is_explicit_about_unavailable_flow_and_free_data(self) -> None:
        profile = DataProfile(
            profile_id="free", universe_file="universe.csv", maximum_candidates=8
        )
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "universe.csv"
            path.write_text("instrument\nAAA\n", encoding="utf-8")
            manifest = provider_manifest(
                profile=profile, universe_path=path,
                cutoff=datetime(2026, 9, 7, tzinfo=ZoneInfo("America/New_York")),
            )
        self.assertEqual(manifest["capabilities"]["order_flow"], "unavailable")
        self.assertEqual(manifest["capabilities"]["option_trade_flow"], "unavailable")
        self.assertTrue(manifest["free_research_data_only"])
        self.assertFalse(manifest["production_grade_claimed"])

    def test_openbb_rejects_path_traversal(self) -> None:
        provider = OpenBBRestProvider(base_url="https://openbb.invalid", timeout_seconds=2)
        with self.assertRaises(DataProviderError):
            provider.get("/../secret")

    def test_openbb_secret_uses_os_credential_store_not_settings_json(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            store = ResearchSettingsStore(self.paths(Path(name)))
            secrets: dict[tuple[str, str], str] = {}

            class Keyring:
                @staticmethod
                def set_password(service, account, value):
                    secrets[(service, account)] = value

                @staticmethod
                def get_password(service, account):
                    return secrets.get((service, account))

            with patch.object(store, "_keyring", return_value=Keyring):
                store.set_openbb_secret("openbb-secret")
                self.assertEqual(store.get_openbb_secret(), "openbb-secret")
                self.assertTrue(store.public_settings()["openbb_secret_saved"])
            settings_path = store.paths.research_settings_file
            persisted = settings_path.read_text(encoding="utf-8") if settings_path.is_file() else ""
            self.assertNotIn("openbb-secret", persisted)

    def test_shadow_files_reject_code_credentials_and_local_paths(self) -> None:
        for path, content in (
            ("tools/run.py", "print('bad')"),
            (
                "skills/market-common-shock/SKILL.md",
                skill_text() + "s" + "k-" + "abcdefghijklmnop\n",
            ),
            (
                "skills/market-common-shock/SKILL.md",
                skill_text() + "/" + "Users/alice/private\n",
            ),
        ):
            with self.subTest(path=path, suffix=content[-20:]):
                with self.assertRaises(SkillVersionError):
                    validate_skill_text(path, content)

    def test_manifest_binds_exact_files_domains_and_main_commit(self) -> None:
        files = {"skills/market-common-shock/SKILL.md": skill_text()}
        manifest = SkillVersionManifest.create(
            version_id="gap-study-1", author="Alice", base_main_sha="a" * 40,
            files=files, description="Clarifies overnight versus regular session.",
            created_at="2026-09-07T00:00:00+00:00",
        )
        self.assertEqual(manifest.author, "alice")
        self.assertEqual(manifest.changed_domains, ("market-common-shock",))
        broken = manifest.as_dict()
        broken["changed_domains"] = ["capital-order-flow"]
        with self.assertRaises(SkillVersionError):
            SkillVersionManifest.from_dict(broken)

    def test_local_registry_install_is_immutable_and_overlays_only_changed_skill(self) -> None:
        files = {"skills/market-common-shock/SKILL.md": skill_text()}
        manifest = SkillVersionManifest.create(
            version_id="market-1", author="alice", base_main_sha="b" * 40,
            files=files, description="One governed change.",
        )
        with tempfile.TemporaryDirectory() as name:
            registry = LocalSkillRegistry(
                root=Path(name) / "registry", package_skills=PACKAGE_ROOT / "skills"
            )
            target = registry.install(manifest=manifest, files=files, commit_sha="c" * 40)
            effective = registry.effective_skills("market-1", "alice")
            self.assertEqual(effective["skills/market-common-shock/SKILL.md"], skill_text())
            self.assertEqual(
                (target / "changed_skills/market-common-shock/SKILL.md").read_bytes(),
                skill_text().encode("utf-8"),
            )
            self.assertIn("skills/price-volume-structure/SKILL.md", effective)
            self.assertTrue(target.is_dir())
            with self.assertRaises(SkillVersionError):
                registry.install(
                    manifest=manifest,
                    files={**files, "skills/price-volume-structure/SKILL.md": skill_text("price-volume-structure")},
                    commit_sha="c" * 40,
                )

    def test_remote_download_rejects_wrong_author_branch(self) -> None:
        config = GitHubRepositoryConfig(
            owner="team", repository="repo", client_id="client"
        )
        client = GitHubSkillClient(config=config, token="token")
        with self.assertRaises(SkillVersionError):
            client.download_version(
                branch="shadow/bob", author="alice", version_id="market-1"
            )

    def test_repository_access_distinguishes_read_only_from_upload(self) -> None:
        config = GitHubRepositoryConfig(
            owner="team", repository="repo", client_id="client"
        )
        client = GitHubSkillClient(config=config, token="token")
        with patch.object(
            client, "_request", return_value={"id": 7, "permissions": {"pull": True, "push": False}}
        ):
            self.assertEqual(client.repository_access(), {"read": True, "write": False})
        with patch.object(
            client, "_request", return_value={"id": 7, "permissions": {"pull": True, "push": True}}
        ):
            self.assertEqual(client.repository_access(), {"read": True, "write": True})

    def test_upload_cannot_overwrite_existing_immutable_version(self) -> None:
        config = GitHubRepositoryConfig(
            owner="team", repository="repo", client_id="client"
        )
        client = GitHubSkillClient(config=config, token="token")
        files = {"skills/market-common-shock/SKILL.md": skill_text()}
        manifest = SkillVersionManifest.create(
            version_id="market-1", author="alice", base_main_sha="b" * 40,
            files=files, description="One governed change.",
        )
        with patch.object(client, "ensure_personal_branch", return_value="shadow/alice"), patch.object(
            client, "content_exists", return_value=True
        ):
            with self.assertRaisesRegex(SkillVersionError, "already exists"):
                client.upload_version(login="alice", manifest=manifest, files=files)

    def test_tampered_local_draft_cannot_be_uploaded(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            registry = LocalSkillRegistry(
                root=Path(name), package_skills=PACKAGE_ROOT / "skills"
            )
            root = registry.save_draft(
                author="alice", draft_id="market-1",
                files={"skills/market-common-shock/SKILL.md": skill_text()},
                base_main_sha="a" * 40,
            )
            (root / "market-common-shock/SKILL.md").write_text(
                skill_text() + "tampered\n", encoding="utf-8"
            )
            with self.assertRaises(SkillVersionError):
                registry.load_draft(author="alice", draft_id="market-1")

    def test_main_skill_registry_contains_eight_skills_and_their_foundations(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            registry = LocalSkillRegistry(
                root=Path(name), package_skills=PACKAGE_ROOT / "skills"
            )
            main = registry.main_version()
        self.assertEqual(len(main["skill_hashes"]), 26)
        self.assertEqual(
            len([path for path in main["skill_hashes"] if path.endswith("/SKILL.md")]),
            8,
        )
        self.assertEqual(
            len([path for path in main["skill_hashes"] if path.endswith("/references/foundations.md")]),
            8,
        )
        self.assertEqual(
            len([path for path in main["skill_hashes"] if path.endswith("/agents/openai.yaml")]),
            8,
        )
        self.assertIn("decision/decision.js", main["skill_hashes"])
        self.assertIn("decision/cases.json", main["skill_hashes"])
        self.assertEqual(main["version_id"], "main")

    def test_shadow_decision_rule_requires_its_cases_and_overlays_main(self) -> None:
        script = (PACKAGE_ROOT / "decision/decision.js").read_text(encoding="utf-8")
        cases = (PACKAGE_ROOT / "decision/cases.json").read_text(encoding="utf-8")
        files = {
            "decision/decision.js": script,
            "decision/cases.json": cases,
        }
        with self.assertRaises(SkillVersionError):
            SkillVersionManifest.create(
                version_id="decision-only",
                author="alice",
                base_main_sha="d" * 40,
                files={"decision/decision.js": script},
                description="Incomplete decision package.",
            )
        manifest = SkillVersionManifest.create(
            version_id="decision-only",
            author="alice",
            base_main_sha="d" * 40,
            files=files,
            description="A tested decision policy.",
        )
        self.assertEqual(manifest.changed_domains, ("decision",))
        with tempfile.TemporaryDirectory() as name:
            registry = LocalSkillRegistry(
                root=Path(name) / "registry", package_skills=PACKAGE_ROOT / "skills"
            )
            target = registry.install(
                manifest=manifest, files=files, commit_sha="e" * 40
            )
            effective = registry.effective_skills("decision-only", "alice")
            self.assertEqual(effective["decision/decision.js"], script)
            self.assertTrue((target / "changed_decision/decision.js").is_file())

    def test_main_version_changes_when_a_foundation_changes(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            package_skills = root / "package" / "skills"
            shutil.copytree(PACKAGE_ROOT / "skills", package_skills)
            registry = LocalSkillRegistry(
                root=root / "registry", package_skills=package_skills
            )
            before = registry.main_version()["version_sha256"]
            foundation = package_skills / "market-common-shock/references/foundations.md"
            foundation.write_text(
                foundation.read_text(encoding="utf-8") + "\nAdditional reviewed source.\n",
                encoding="utf-8",
            )
            after = registry.main_version()["version_sha256"]
        self.assertNotEqual(before, after)


if __name__ == "__main__":
    unittest.main()
