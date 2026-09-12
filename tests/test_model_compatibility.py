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
            "SystemRoot": r"C:\Windows",
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

    def test_windows_cmd_launcher_is_explicit_and_prompt_stays_on_stdin(self) -> None:
        """Putting the prompt in a command string would permit shell interpolation."""

        profile = ModelProfile(
            profile_id="claude", protocol="claude-code", base_url="",
            model="subscription-default",
        )
        prompt = 'frozen evidence & echo "not a command"'
        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"structured_output":{"status":"ready"}}', stderr="",
        )
        environment = {
            "SystemRoot": r"C:\Windows",
            "PATH": r"C:\Windows\System32",
        }
        launcher = r"C:\Users\李 小明\AppData\Roaming\npm\claude.cmd"
        with patch.object(sys, "platform", "win32"), patch.dict(
            os.environ, environment, clear=True
        ), patch.object(
            model_backends, "_local_cli", return_value=launcher
        ), patch.object(
            model_backends.subprocess, "run", return_value=completed
        ) as run:
            result, _ = call_structured(
                profile=profile, secret="", prompt=prompt, schema=READY_SCHEMA
            )

        command = run.call_args.args[0]
        self.assertEqual(result, {"status": "ready"})
        self.assertEqual(command[:4], [r"C:\Windows\System32\cmd.exe", "/d", "/s", "/c"])
        self.assertEqual(command[4], launcher)
        self.assertNotIn(prompt, command)
        self.assertEqual(run.call_args.kwargs["input"], prompt)
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "replace")
        self.assertFalse(run.call_args.kwargs["shell"])

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

    def test_model_profile_identity_tracks_effective_protocol_policy(self) -> None:
        """Changing request capabilities must invalidate policy-bound cached work."""

        profile = relay_profile()
        original = profile.identity()
        capabilities = getattr(model_backends, "_PROTOCOL_CAPABILITIES", None)
        self.assertIsNotNone(capabilities)
        if capabilities is None:
            return
        capability = capabilities[profile.protocol]
        changed = replace(capability, policy_version=capability.policy_version + 1)
        with patch.dict(
            model_backends._PROTOCOL_CAPABILITIES,
            {profile.protocol: changed},
        ):
            self.assertNotEqual(profile.identity(), original)


if __name__ == "__main__":
    unittest.main()
