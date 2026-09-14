from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

import httpx

from shaq_daily_oracle import model_backends
from shaq_daily_oracle.model_backends import (
    ModelBackendError,
    ModelProfile,
    call_structured,
    probe_model_profile,
)
from shaq_daily_oracle.research_batch import ContentAddressedModelCache


READY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status"],
    "properties": {"status": {"type": "string", "const": "ready"}},
}


def relay_profile() -> ModelProfile:
    return ModelProfile(
        profile_id="relay",
        protocol="openai-chat-completions",
        base_url="https://relay.invalid/v1",
        model="relay-model",
        output_mode="local_validated",
    )


class JsonResponse:
    def __init__(self, value: object) -> None:
        self.value = value

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.value


class MalformedJsonResponse(JsonResponse):
    def json(self) -> object:
        raise json.JSONDecodeError("invalid", "not-json", 0)


class ModelCompatibilityTests(unittest.TestCase):
    def test_openai_sdk_http_and_timeout_errors_use_safe_structured_diagnostics(self) -> None:
        class SDKError(Exception):
            def __init__(self, status: int) -> None:
                super().__init__("Authorization: Bearer sdk-secret")
                self.status_code = status
                self.code = "invalid_api_key"
                self.body = {"error": {"code": "invalid_api_key", "message": "bad sdk-secret"}}
                self.response = httpx.Response(
                    status, request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
                    headers={"x-request-id": f"sdk-{status}"},
                )

        class Responses:
            failure: Exception

            def create(self, **request):
                raise self.failure

        class Client:
            def __init__(self, **kwargs):
                self.responses = Responses()

        profile = ModelProfile(
            profile_id="official", protocol="openai-responses",
            base_url="https://api.openai.com/v1", model="gpt-test",
        )
        for failure, expected_kind, expected_status in [
            *((SDKError(status), "http", status) for status in (400, 401, 403, 429)),
            (TimeoutError("sdk-secret timeout"), "timeout", None),
        ]:
            with self.subTest(failure=type(failure).__name__, status=expected_status), patch(
                "openai.OpenAI", Client
            ):
                Responses.failure = failure
                with self.assertRaises(ModelBackendError) as raised:
                    model_backends._openai_responses_call(
                        profile=profile, secret="sdk-secret", prompt="packet", schema=READY_SCHEMA,
                    )
            diagnostic = raised.exception.diagnostic
            self.assertEqual(diagnostic["kind"], expected_kind)
            self.assertEqual(diagnostic["status"], expected_status)
            self.assertNotIn("sdk-secret", str(raised.exception))
            self.assertNotIn("sdk-secret", json.dumps(diagnostic))
            if expected_status:
                self.assertEqual(diagnostic["request_id"], f"sdk-{expected_status}")

    def test_http_diagnostics_keep_bounded_provider_details_and_request_id(self) -> None:
        for status in (400, 401, 403, 429):
            with self.subTest(status=status):
                response = httpx.Response(
                    status,
                    request=httpx.Request("POST", "https://relay.invalid/v1/chat/completions"),
                    headers={"x-request-id": f"req-{status}", "authorization": "Bearer header-secret"},
                    json={"error": {"code": "bad_request", "message": "invalid token body-secret"}},
                )
                with patch("httpx.post", return_value=response):
                    with self.assertRaises(ModelBackendError) as raised:
                        model_backends._http_post_json(
                            url="https://relay.invalid/v1/chat/completions",
                            headers={"Authorization": "Bearer body-secret"}, payload={}, timeout=1,
                        )
                diagnostic = raised.exception.diagnostic
                self.assertEqual(diagnostic["status"], status)
                self.assertEqual(diagnostic["provider_code"], "bad_request")
                self.assertEqual(diagnostic["request_id"], f"req-{status}")
                self.assertNotIn("body-secret", json.dumps(diagnostic))
                self.assertNotIn("header-secret", json.dumps(diagnostic))

    def test_http_diagnostics_handle_non_json_and_timeout_without_secrets(self) -> None:
        response = httpx.Response(
            400, request=httpx.Request("POST", "https://relay.invalid/v1/chat/completions"),
            headers={"request-id": "req-text"}, text="token=body-secret " + "x" * 1000,
        )
        with patch("httpx.post", return_value=response):
            with self.assertRaises(ModelBackendError) as malformed:
                model_backends._http_post_json(
                    url="https://relay.invalid/v1/chat/completions",
                    headers={"x-api-key": "body-secret"}, payload={}, timeout=1,
                )
        self.assertEqual(malformed.exception.diagnostic["provider_message"], "")
        self.assertEqual(malformed.exception.diagnostic["request_id"], "req-text")

        with patch("httpx.post", side_effect=httpx.ReadTimeout("Bearer body-secret")):
            with self.assertRaises(ModelBackendError) as timeout:
                model_backends._http_post_json(
                    url="https://relay.invalid/v1/chat/completions",
                    headers={"Authorization": "Bearer body-secret"}, payload={}, timeout=1,
                )
        self.assertEqual(timeout.exception.diagnostic["kind"], "timeout")
        self.assertNotIn("body-secret", str(timeout.exception))
    def test_relay_payload_omits_sampling_controls_and_records_effective_policy(self) -> None:
        """Removing the protocol policy must not silently restore temperature/top_p."""

        response = JsonResponse({
            "id": "chat-1",
            "model": "relay-model",
            "choices": [{"message": {"content": '{"status":"ready"}'}}],
        })
        with patch("httpx.post", return_value=response) as post:
            result, audit = call_structured(
                profile=relay_profile(), secret="relay-secret", prompt="packet",
                schema=READY_SCHEMA,
            )

        payload = post.call_args.kwargs["json"]
        self.assertEqual(result, {"status": "ready"})
        self.assertNotIn("temperature", payload)
        self.assertNotIn("top_p", payload)
        self.assertEqual(audit["request_policy"]["sampling_parameters"], [])

    def test_relay_transport_and_format_failures_do_not_switch_provider(self) -> None:
        """A failed relay call must stay failed instead of falling back elsewhere."""

        malformed = MalformedJsonResponse(None)
        failures = (
            malformed,
            httpx.HTTPStatusError(
                "401 x-api-key=do-not-leak",
                request=httpx.Request("POST", "https://relay.invalid/v1/chat/completions"),
                response=httpx.Response(401),
            ),
            httpx.HTTPStatusError(
                "429 Authorization: Bearer do-not-leak",
                request=httpx.Request("POST", "https://relay.invalid/v1/chat/completions"),
                response=httpx.Response(429),
            ),
            httpx.ReadTimeout("timed out; token=do-not-leak"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), patch(
                "httpx.post", return_value=failure
            ) as post:
                if isinstance(failure, Exception):
                    post.side_effect = failure
                with self.assertRaises(ModelBackendError) as raised:
                    call_structured(
                        profile=relay_profile(), secret="do-not-leak", prompt="packet",
                        schema=READY_SCHEMA,
                    )
            self.assertEqual(post.call_count, 1)
            actual_url = (
                post.call_args.kwargs.get("url")
                or post.call_args.args[0]
            )
            self.assertEqual(actual_url, "https://relay.invalid/v1/chat/completions")
            self.assertNotIn("do-not-leak", str(raised.exception))

    def test_unexpected_http_error_does_not_echo_bearer_secret(self) -> None:
        """A transport that mentions only the raw bearer value must still be redacted."""

        with patch(
            "httpx.post", side_effect=RuntimeError("transport rejected relay-secret")
        ):
            with self.assertRaises(ModelBackendError) as raised:
                call_structured(
                    profile=relay_profile(), secret="relay-secret", prompt="packet",
                    schema=READY_SCHEMA,
                )
        self.assertNotIn("relay-secret", str(raised.exception))

    def test_windows_cli_environment_keeps_system_paths_but_filters_secrets(self) -> None:
        """Dropping a Windows process variable must fail without exposing app secrets."""

        user = PureWindowsPath('C:/') / 'Users' / '李 小明'
        retained = {
            "USERPROFILE": str(user),
            "APPDATA": str(user / 'AppData' / 'Roaming'),
            "LOCALAPPDATA": str(user / 'AppData' / 'Local'),
            "SYSTEMROOT": r"C:\Windows",
            "WINDIR": r"C:\Windows",
            "TEMP": str(user / 'AppData' / 'Local' / 'Temp'),
            "TMP": str(user / 'AppData' / 'Local' / 'Temp'),
            "PATH": r"C:\Windows\System32",
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "LANG": "zh_HK.UTF-8",
            "LANGUAGE": "zh_HK:zh",
            "LC_ALL": "zh_HK.UTF-8",
            "LC_CTYPE": "UTF-8",
        }
        source = {
            **retained,
            "OPENAI_API_KEY": "openai-secret",
            "ANTHROPIC_API_KEY": "anthropic-secret",
            "CUSTOM_SECRET": "other-secret",
        }
        with patch.dict(os.environ, source, clear=True):
            actual = model_backends._local_cli_environment()
        self.assertEqual(actual, retained)

    def test_windows_prefers_native_claude_exe_from_userprofile(self) -> None:
        """A PATH launcher must not hide the official native user installation."""

        with tempfile.TemporaryDirectory() as name:
            user = Path(name) / "李 小明"
            native = user / ".local" / "bin" / "claude.exe"
            native.parent.mkdir(parents=True)
            native.write_bytes(b"native")
            native.chmod(0o700)
            profile = ModelProfile(
                profile_id="claude", protocol="claude-code", base_url="",
                model="subscription-default",
            )
            environment = {
                "USERPROFILE": str(user),
                "APPDATA": str(user / "AppData" / "Roaming"),
                "LOCALAPPDATA": str(user / "AppData" / "Local"),
            }
            with patch.object(sys, "platform", "win32"), patch.dict(
                os.environ, environment, clear=True
            ), patch.object(
                model_backends.shutil, "which", return_value=str(user / "claude.cmd")
            ), patch.object(
                model_backends.subprocess, "run", return_value=subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="2.1.211 (Claude Code)\n", stderr=""
                )
            ):
                actual = model_backends._local_cli(profile)
        self.assertEqual(actual, str(native))

    def test_windows_codex_discovers_bounded_versioned_vendor_install_and_skips_stale(self) -> None:
        """Removing the bounded version scan or first-usable fallback must fail."""

        with tempfile.TemporaryDirectory() as name:
            local = Path(name) / "Users" / "李 小明" / "AppData" / "Local"
            vendor = local / "OpenAI" / "Codex" / "bin"
            stale = vendor / "z-stale-build" / "codex.exe"
            usable = vendor / "a-changing-opaque-id" / "codex.exe"
            for path in (stale, usable):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
            profile = ModelProfile(
                profile_id="codex", protocol="codex-cli", base_url="",
                model="subscription-default",
            )
            calls = []

            def identify(command, **kwargs):
                calls.append((command, kwargs))
                if Path(command[0]) == stale:
                    return subprocess.CompletedProcess(command, 1, "", "stale")
                return subprocess.CompletedProcess(command, 0, "codex-cli 0.116.0\n", "")

            with patch.object(sys, "platform", "win32"), patch.dict(
                os.environ, {"LOCALAPPDATA": str(local)}, clear=True
            ), patch.object(model_backends.shutil, "which", return_value=None), patch.object(
                model_backends.subprocess, "run", side_effect=identify
            ):
                actual = model_backends._local_cli(profile)

        self.assertEqual(actual, str(usable))
        self.assertEqual([row[0] for row in calls], [
            [str(stale), "--version"], [str(usable), "--version"],
        ])
        self.assertTrue(all(row[1]["shell"] is False for row in calls))

    def test_windows_rejects_claude_desktop_alias_before_execution(self) -> None:
        """A WindowsApps PATH alias must never be launched as Claude Code."""

        profile = ModelProfile(
            profile_id="claude", protocol="claude-code", base_url="",
            model="subscription-default",
        )
        alias = str(PureWindowsPath("C:/", "Users", "李 小明", "AppData", "Local",
                                    "Microsoft", "WindowsApps", "Claude.exe"))
        with patch.object(sys, "platform", "win32"), patch.dict(
            os.environ, {}, clear=True
        ), patch.object(model_backends.shutil, "which", return_value=alias), patch.object(
            model_backends.subprocess, "run"
        ) as run:
            with self.assertRaisesRegex(ModelBackendError, "Claude Desktop"):
                model_backends._local_cli(profile)
        run.assert_not_called()

    def test_windows_reports_unusable_executable_separately_from_missing(self) -> None:
        """A present binary with an invalid identity must not be reported as absent."""

        profile = ModelProfile(
            profile_id="codex", protocol="codex-cli", base_url="",
            model="subscription-default",
        )
        with tempfile.TemporaryDirectory() as name:
            local = Path(name) / "Local"
            candidate = local / "OpenAI/Codex/bin/build/codex.exe"
            candidate.parent.mkdir(parents=True)
            candidate.write_bytes(b"fixture")
            with patch.object(sys, "platform", "win32"), patch.dict(
                os.environ, {"LOCALAPPDATA": str(local)}, clear=True
            ), patch.object(model_backends.shutil, "which", return_value=None), patch.object(
                model_backends.subprocess, "run", return_value=subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="not codex\n", stderr=""
                )
            ):
                with self.assertRaisesRegex(ModelBackendError, "无法使用") as unusable:
                    model_backends._local_cli(profile)
        self.assertEqual(unusable.exception.diagnostic["kind"], "executable_unusable")

        with patch.object(sys, "platform", "win32"), patch.dict(
            os.environ, {}, clear=True
        ), patch.object(model_backends.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ModelBackendError, "未找到") as missing:
                model_backends._local_cli(profile)
        self.assertEqual(missing.exception.diagnostic["kind"], "executable_missing")

    def test_windows_legacy_cmd_launcher_is_rejected_before_execution(self) -> None:
        """A legacy shim must not place model arguments behind cmd.exe parsing."""

        profile = ModelProfile(
            profile_id="claude", protocol="claude-code", base_url="",
            model="subscription-default",
        )
        launcher = str(PureWindowsPath('C:/') / 'Users' / '李 小明' /
                       'AppData' / 'Roaming' / 'npm' / 'claude.cmd')
        with patch.object(sys, "platform", "win32"), patch.object(
            model_backends, "_local_cli", return_value=launcher
        ), patch.object(
            model_backends.subprocess, "run"
        ) as run:
            with self.assertRaisesRegex(ModelBackendError, r"原生.*\.exe"):
                call_structured(
                    profile=profile, secret="", prompt="packet & not-a-command",
                    schema=READY_SCHEMA,
                )
        run.assert_not_called()

    def test_unsupported_windows_launcher_is_rejected_before_execution(self) -> None:
        """Unexpected script launchers must not gain implicit shell execution."""

        profile = ModelProfile(
            profile_id="claude", protocol="claude-code", base_url="",
            model="subscription-default",
        )
        with patch.object(sys, "platform", "win32"), patch.object(
            model_backends, "_local_cli", return_value=r"C:\temp\claude.ps1"
        ), patch.object(model_backends.subprocess, "run") as run:
            with self.assertRaisesRegex(ModelBackendError, "launcher"):
                call_structured(
                    profile=profile, secret="", prompt="packet", schema=READY_SCHEMA
                )
        run.assert_not_called()

    def test_claude_probe_checks_auth_then_performs_structured_call(self) -> None:
        """Replacing the structured call with --version must make this test fail."""

        profile = ModelProfile(
            profile_id="claude", protocol="claude-code", base_url="",
            model="subscription-default",
        )
        auth = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"loggedIn": True, "authMethod": "claude.ai"}), stderr="",
        )
        structured = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({
                "session_id": "probe-session",
                "structured_output": {"status": "ready"},
            }),
            stderr="",
        )
        for platform_name, executable in (("darwin", "claude"), ("win32", "claude.exe")):
            with self.subTest(platform=platform_name), patch.object(
                sys, "platform", platform_name
            ), patch.object(
                model_backends, "_local_cli", return_value=executable
            ), patch.object(
                model_backends.subprocess, "run", side_effect=[auth, structured]
            ) as run:
                audit = probe_model_profile(profile=profile, secret="")

            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[0].args[0][0], executable)
            self.assertEqual(run.call_args_list[0].args[0][1:], ["auth", "status"])
            self.assertIn("-p", run.call_args_list[1].args[0])
            self.assertNotIn("--version", run.call_args_list[0].args[0])
            self.assertEqual(audit["connection"], "local-subscription-ready")
            self.assertEqual(audit["schema_enforcement"], "cli-json-schema")

    def test_claude_schema_call_rejects_free_text_result_fallback(self) -> None:
        """A JSON-looking result field must not replace missing structured_output."""

        profile = ModelProfile(
            profile_id="claude", protocol="claude-code", base_url="",
            model="subscription-default",
        )
        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"result": '{"status":"ready"}'}), stderr="",
        )
        for platform_name, executable in (("darwin", "claude"), ("win32", "claude.exe")):
            with self.subTest(platform=platform_name), patch.object(
                sys, "platform", platform_name
            ), patch.object(
                model_backends, "_local_cli", return_value=executable
            ), patch.object(
                model_backends.subprocess, "run", return_value=completed
            ):
                with self.assertRaisesRegex(ModelBackendError, "structured_output"):
                    call_structured(
                        profile=profile, secret="", prompt="packet", schema=READY_SCHEMA
                    )

    def test_cli_errors_are_redacted_and_keep_failure_stage(self) -> None:
        """CLI stderr must identify auth/call stage without echoing credentials."""

        profile = ModelProfile(
            profile_id="claude", protocol="claude-code", base_url="",
            model="subscription-default",
        )
        failed_auth = subprocess.CompletedProcess(
            args=[], returncode=1,
            stdout=json.dumps({"loggedIn": False}),
            stderr="ANTHROPIC_API_KEY=do-not-leak login expired",
        )
        for platform_name, executable in (("darwin", "claude"), ("win32", "claude.exe")):
            with self.subTest(platform=platform_name), patch.object(
                sys, "platform", platform_name
            ), patch.object(
                model_backends, "_local_cli", return_value=executable
            ), patch.object(
                model_backends.subprocess, "run", return_value=failed_auth
            ):
                with self.assertRaisesRegex(ModelBackendError, "登录") as raised:
                    probe_model_profile(profile=profile, secret="")
            self.assertNotIn("do-not-leak", str(raised.exception))

    def test_local_auth_status_distinguishes_unsigned_malformed_and_timeout(self) -> None:
        """Malformed/timeout status checks must not be mislabeled as signed-out users."""

        profile = ModelProfile(
            profile_id="claude", protocol="claude-code", base_url="",
            model="subscription-default",
        )
        cases = [
            (
                subprocess.CompletedProcess([], 1, json.dumps({"loggedIn": False}), ""),
                "尚未登录", "authentication",
            ),
            (subprocess.CompletedProcess([], 0, "not-json", ""), "JSON", "status_invalid"),
            (subprocess.TimeoutExpired(["claude", "auth", "status"], 10), "超时", "timeout"),
        ]
        for outcome, message, kind in cases:
            run_options = (
                {"side_effect": outcome}
                if isinstance(outcome, BaseException)
                else {"return_value": outcome}
            )
            with self.subTest(kind=kind), patch.object(
                model_backends, "_local_cli", return_value="claude.exe"
            ), patch.object(model_backends.subprocess, "run", **run_options), patch.object(
                model_backends, "call_structured",
                return_value=({"status": "ready"}, {"backend": "fixture"}),
            ) as structured:
                with self.assertRaisesRegex(ModelBackendError, message) as raised:
                    probe_model_profile(profile=profile, secret="")
                self.assertIsInstance(raised.exception.diagnostic, dict)
                if isinstance(raised.exception.diagnostic, dict):
                    self.assertEqual(raised.exception.diagnostic["kind"], kind)
                structured.assert_not_called()

    def test_abnormal_auth_status_never_returns_cli_tokens_or_oauth_urls(self) -> None:
        leaks = [
            json.dumps({"access_token": "quoted-json-token", "url": "https://oauth.example/device"}),
            'Open "https://oauth.example/device?code=oauth-secret-code" to continue',
        ]
        for protocol, output in (("claude-code", leaks[0]), ("codex-cli", leaks[1])):
            profile = ModelProfile(profile_id="local", protocol=protocol, base_url="",
                                   model="subscription-default")
            completed = subprocess.CompletedProcess([], 7, output, output)
            with self.subTest(protocol=protocol), patch.object(
                model_backends, "_local_cli", return_value="local.exe"
            ), patch.object(model_backends.subprocess, "run", return_value=completed), patch.object(
                model_backends, "call_structured"
            ) as structured:
                with self.assertRaisesRegex(ModelBackendError, "登录状态异常") as raised:
                    probe_model_profile(profile=profile, secret="")
            message = str(raised.exception)
            self.assertNotIn("quoted-json-token", message)
            self.assertNotIn("oauth.example", message)
            self.assertNotIn("oauth-secret-code", message)
            self.assertEqual(raised.exception.diagnostic, {
                "kind": "status_invalid", "protocol": protocol,
                "stage": "authentication-status",
            })
            structured.assert_not_called()

    def test_codex_unsigned_status_offers_codex_login_without_model_call(self) -> None:
        profile = ModelProfile(
            profile_id="codex", protocol="codex-cli", base_url="",
            model="subscription-default",
        )
        unsigned = subprocess.CompletedProcess(
            ["codex.exe", "login", "status"], 1, "Not logged in\n", ""
        )
        with patch.object(
            model_backends, "_local_cli", return_value="codex.exe"
        ), patch.object(
            model_backends.subprocess, "run", return_value=unsigned
        ), patch.object(model_backends, "call_structured") as structured:
            with self.assertRaisesRegex(ModelBackendError, "尚未登录") as raised:
                probe_model_profile(profile=profile, secret="")
        self.assertEqual(raised.exception.diagnostic, {
            "kind": "authentication", "protocol": "codex-cli",
            "login_protocol": "codex-cli",
        })
        structured.assert_not_called()

    def test_local_login_action_uses_only_fixed_native_commands_and_returns_no_raw_output(self) -> None:
        """User-controlled data must never enter the browser-login command or receipt."""

        login = getattr(model_backends, "begin_local_subscription_login", None)
        self.assertIsNotNone(login)
        if login is None:
            return
        for protocol, executable, expected in (
            ("codex-cli", "codex.exe", ["codex.exe", "login"]),
            ("claude-code", "claude.exe", ["claude.exe", "auth", "login"]),
        ):
            profile = ModelProfile(
                profile_id="local", protocol=protocol, base_url="",
                model="subscription-default",
            )
            completed = subprocess.CompletedProcess(
                expected, 0, "https://oauth.example/do-not-persist", "token=do-not-persist"
            )
            with self.subTest(protocol=protocol), patch.object(
                model_backends, "_local_cli", return_value=executable
            ), patch.object(model_backends.subprocess, "run", return_value=completed) as run:
                receipt = login(profile)
            self.assertEqual(receipt, {"protocol": protocol, "status": "login-completed"})
            self.assertEqual(run.call_args.args[0], expected)
            self.assertIs(run.call_args.kwargs["shell"], False)
            self.assertGreater(run.call_args.kwargs["timeout"], 0)
            self.assertNotIn("do-not-persist", json.dumps(receipt))

        api = ModelProfile(
            profile_id="api", protocol="openai-responses",
            base_url="https://api.openai.com/v1", model="gpt-test",
        )
        with patch.object(model_backends.subprocess, "run") as run:
            with self.assertRaisesRegex(ModelBackendError, "本机订阅"):
                login(api)
        run.assert_not_called()

    def test_profile_identity_stays_stable_when_request_policy_changes(self) -> None:
        """Request transport changes must not split historical model/account series."""

        profile = relay_profile()
        original = profile.identity()
        policy_identity = getattr(profile, "request_policy_identity", None)
        self.assertIsNotNone(policy_identity)
        if policy_identity is None:
            return
        original_policy = policy_identity()
        capability = model_backends._PROTOCOL_CAPABILITIES[profile.protocol]
        changed = replace(capability, policy_version=capability.policy_version + 1)
        with patch.dict(
            model_backends._PROTOCOL_CAPABILITIES,
            {profile.protocol: changed},
        ):
            self.assertEqual(profile.identity(), original)
            self.assertNotEqual(profile.request_policy_identity(), original_policy)

    def test_model_cache_routing_tracks_policy_without_changing_frozen_identity(self) -> None:
        """A new request policy must miss old cache while preserving the model series hash."""

        profile = relay_profile()
        calls = []

        def caller(**kwargs):
            calls.append(kwargs["profile"].identity())
            return {"status": "ready"}, {
                "profile_sha256": kwargs["profile"].identity(),
                "request_policy": {"tools": []},
            }

        with tempfile.TemporaryDirectory() as name:
            cache = ContentAddressedModelCache(Path(name))
            first = cache.call(
                profile=profile, secret="", prompt="packet", schema=READY_SCHEMA,
                caller=caller,
            )
            capability = model_backends._PROTOCOL_CAPABILITIES[profile.protocol]
            changed = replace(capability, policy_version=capability.policy_version + 1)
            with patch.dict(
                model_backends._PROTOCOL_CAPABILITIES,
                {profile.protocol: changed},
            ):
                second = cache.call(
                    profile=profile, secret="", prompt="packet", schema=READY_SCHEMA,
                    caller=caller,
                )
        self.assertFalse(first[2])
        self.assertFalse(second[2])
        self.assertEqual(first[1]["cache_key"], second[1]["cache_key"])
        self.assertEqual(calls, [profile.identity(), profile.identity()])

    def test_model_mismatch_error_does_not_echo_returned_model_text(self) -> None:
        """An endpoint-controlled model field must not be reflected in user-facing errors."""

        response = JsonResponse({
            "id": "chat-1",
            "model": "wrong-model-do-not-leak",
            "choices": [{"message": {"content": '{"status":"ready"}'}}],
        })
        with patch("httpx.post", return_value=response):
            with self.assertRaisesRegex(ModelBackendError, "different model") as raised:
                call_structured(
                    profile=relay_profile(), secret="do-not-leak", prompt="packet",
                    schema=READY_SCHEMA,
                )
        self.assertNotIn("do-not-leak", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
