from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
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

        retained = {
            "USERPROFILE": r"C:\Users\李 小明",
            "APPDATA": r"C:\Users\李 小明\AppData\Roaming",
            "LOCALAPPDATA": r"C:\Users\李 小明\AppData\Local",
            "SYSTEMROOT": r"C:\Windows",
            "WINDIR": r"C:\Windows",
            "TEMP": r"C:\Users\李 小明\AppData\Local\Temp",
            "TMP": r"C:\Users\李 小明\AppData\Local\Temp",
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
            ):
                actual = model_backends._local_cli(profile)
        self.assertEqual(actual, str(native))

    def test_windows_legacy_cmd_launcher_is_rejected_before_execution(self) -> None:
        """A legacy shim must not place model arguments behind cmd.exe parsing."""

        profile = ModelProfile(
            profile_id="claude", protocol="claude-code", base_url="",
            model="subscription-default",
        )
        launcher = r"C:\Users\李 小明\AppData\Roaming\npm\claude.cmd"
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
        auth = subprocess.CompletedProcess(args=[], returncode=0, stdout="authenticated", stderr="")
        structured = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({
                "session_id": "probe-session",
                "structured_output": {"status": "ready"},
            }),
            stderr="",
        )
        with patch.object(
            model_backends, "_local_cli", return_value="/usr/local/bin/claude"
        ), patch.object(
            model_backends.subprocess, "run", side_effect=[auth, structured]
        ) as run:
            audit = probe_model_profile(profile=profile, secret="")

        self.assertEqual(run.call_count, 2)
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
        with patch.object(
            model_backends, "_local_cli", return_value="/usr/local/bin/claude"
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
            stdout="", stderr="ANTHROPIC_API_KEY=do-not-leak login expired",
        )
        with patch.object(
            model_backends, "_local_cli", return_value="/usr/local/bin/claude"
        ), patch.object(
            model_backends.subprocess, "run", return_value=failed_auth
        ):
            with self.assertRaisesRegex(ModelBackendError, "登录") as raised:
                probe_model_profile(profile=profile, secret="")
        self.assertNotIn("do-not-leak", str(raised.exception))

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
