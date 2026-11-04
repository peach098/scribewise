"""Settings validation. Values are synthetic placeholders."""

from __future__ import annotations

import base64
from datetime import time
from typing import Any

import pytest
from pydantic import ValidationError

from scribe.config import Settings

KEY_B64 = base64.b64encode(b"\x00" * 32).decode()


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://u:p@localhost:5432/db",
        "master_key": KEY_B64,
        "bot_token": "placeholder",
        "tg_api_id": 1,
        "tg_api_hash": "placeholder",
        **overrides,
    }
    return Settings(_env_file=None, **values)


def test_defaults() -> None:
    s = _settings()
    assert s.scheduler_tick_s == 5
    assert s.health_port == 8080
    assert s.test_fast_clock is False
    assert (s.delay_min_s, s.delay_max_s) == (120, 600)
    assert (s.work_start, s.work_end) == (time(9, 0), time(19, 0))
    assert s.working_days == [0, 1, 2, 3, 4]
    assert s.grouping_window_s == 60
    assert s.max_contact_profiles == 20
    assert s.message_retention_days == 30
    assert s.style_import_months == 6
    assert (s.profile_examples_contact, s.profile_examples_global) == (20, 50)
    assert s.min_style_samples == 50
    assert s.profile_max_samples == 400
    assert (s.profile_group_samples_per_chat, s.profile_group_samples_share) == (40, 0.3)
    assert s.work_account_import_max_chats == 50
    assert s.master_key_bytes == b"\x00" * 32


def test_reply_gate_defaults() -> None:
    s = _settings()
    assert s.skip_candidate_max_chars == 2000
    assert s.skip_min_confidence == 0.8
    assert s.skip_context_messages == 8
    assert (s.skip_habit_window, s.skip_habit_min_replies) == (5, 3)
    assert s.skip_habit_reply_window_min == 30
    assert s.skipped_list_days == 7


def test_skip_min_confidence_must_be_a_share() -> None:
    with pytest.raises(ValidationError):
        _settings(skip_min_confidence=1.5)


def test_telegram_login_defaults() -> None:
    s = _settings()
    assert s.tg_qr_max_codes == 5
    assert s.tg_login_password_timeout_s == 600
    assert s.tg_qr_same_device_link is False
    assert s.tg_device_model == "Scribe Agent"
    assert s.tg_system_version == "server"
    assert s.tg_app_version


def test_telegram_login_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRIBE_TG_QR_MAX_CODES", "3")
    monkeypatch.setenv("SCRIBE_TG_QR_SAME_DEVICE_LINK", "true")
    s = _settings()
    assert s.tg_qr_max_codes == 3
    assert s.tg_qr_same_device_link is True
    with pytest.raises(ValidationError):
        _settings(tg_qr_max_codes=0)


def test_reads_prefixed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRIBE_DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("SCRIBE_MASTER_KEY", KEY_B64)
    monkeypatch.setenv("SCRIBE_BOT_TOKEN", "placeholder")
    monkeypatch.setenv("SCRIBE_TG_API_ID", "12345")
    monkeypatch.setenv("SCRIBE_TG_API_HASH", "placeholder")
    monkeypatch.setenv("SCRIBE_SCHEDULER_TICK_S", "2")
    s = Settings(_env_file=None)
    assert s.tg_api_id == 12345
    assert s.scheduler_tick_s == 2


def test_secrets_are_not_in_repr() -> None:
    s = _settings(bot_token="SYNTHETIC-BOT-TOKEN")
    assert "SYNTHETIC-BOT-TOKEN" not in repr(s)
    assert KEY_B64 not in repr(s)


@pytest.mark.parametrize(
    "key",
    ["not base64!!", base64.b64encode(b"\x00" * 16).decode(), base64.b64encode(b"x" * 33).decode()],
)
def test_master_key_must_be_32_bytes(key: str) -> None:
    with pytest.raises(ValidationError):
        _settings(master_key=key)


@pytest.mark.parametrize(
    "overrides",
    [
        {"delay_min_s": 700, "delay_max_s": 600},
        {"work_start": "19:00", "work_end": "09:00"},
        {"working_days": []},
        {"working_days": [7]},
        {"log_level": "verbose"},
    ],
)
def test_invalid_values_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _settings(**overrides)


def test_access_gate_defaults() -> None:
    s = _settings()
    assert s.access_gate is False
    assert s.operator_telegram_ids == []
    assert s.subscription_contact == ""
    assert s.access_refusal_cooldown_s == 3600


def test_access_gate_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRIBE_ACCESS_GATE", "true")
    monkeypatch.setenv("SCRIBE_OPERATOR_TELEGRAM_IDS", "900000001,900000002")
    monkeypatch.setenv("SCRIBE_SUBSCRIPTION_CONTACT", "@billing_example")
    monkeypatch.setenv("SCRIBE_ACCESS_REFUSAL_COOLDOWN_S", "60")
    s = _settings()
    assert s.access_gate is True
    assert s.operator_telegram_ids == [900000001, 900000002]
    assert s.subscription_contact == "@billing_example"
    assert s.access_refusal_cooldown_s == 60


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", []), (" 900000001 , 900000002 ,", [900000001, 900000002]), ("[900000003]", [900000003])],
)
def test_operator_telegram_ids_parsing(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: list[int]
) -> None:
    monkeypatch.setenv("SCRIBE_OPERATOR_TELEGRAM_IDS", raw)
    assert _settings().operator_telegram_ids == expected


def test_access_refusal_cooldown_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        _settings(access_refusal_cooldown_s=0)


def test_jira_defaults() -> None:
    s = _settings()
    assert s.jira_dc_min_version == "10.3"
    assert s.jira_allowed_private_hosts == []
    assert s.jira_sync_interval_s == 300
    assert s.jira_sync_concurrency == 5
    assert s.jira_lookback_days == 30
    assert s.jira_first_scan_max_issues == 1000


def test_jira_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRIBE_JIRA_ALLOWED_PRIVATE_HOSTS", "jira.corp.example, 10.20.0.0/16")
    monkeypatch.setenv("SCRIBE_JIRA_DC_MIN_VERSION", "11.3")
    s = _settings()
    assert s.jira_allowed_private_hosts == ["jira.corp.example", "10.20.0.0/16"]
    assert s.jira_dc_min_version == "11.3"


@pytest.mark.parametrize("version", ["8.13", "ten", "", "10.x"])
def test_jira_min_version_rejected(version: str) -> None:
    with pytest.raises(ValidationError):
        _settings(jira_dc_min_version=version)


def test_jira_sync_interval_floor() -> None:
    with pytest.raises(ValidationError):
        _settings(jira_sync_interval_s=30)


def test_earlier_search_defaults() -> None:
    s = _settings()
    assert s.earlier_search_enabled is True
    assert s.earlier_search_timeout_s == 6


@pytest.mark.parametrize("timeout", [0, 0.5, 9.5, 10])
def test_earlier_search_timeout_bounds(timeout: float) -> None:
    with pytest.raises(ValidationError):
        _settings(earlier_search_timeout_s=timeout)


def test_earlier_search_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRIBE_EARLIER_SEARCH_ENABLED", "false")
    monkeypatch.setenv("SCRIBE_EARLIER_SEARCH_TIMEOUT_S", "4")
    s = _settings()
    assert s.earlier_search_enabled is False
    assert s.earlier_search_timeout_s == 4


def test_release_notes_defaults() -> None:
    """specs/011-release-notes data-model.md "Settings"."""
    s = _settings()
    assert str(s.release_notes_dir) == "release-notes"
    assert s.release_notes_auto is True
    assert s.release_notes_rate_per_s == 10
    assert s.release_notes_max_attempts == 5


@pytest.mark.parametrize(
    "overrides",
    [
        {"release_notes_rate_per_s": 0},
        {"release_notes_rate_per_s": 26},
        {"release_notes_max_attempts": 0},
    ],
)
def test_release_notes_bounds(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _settings(**overrides)


def test_release_notes_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRIBE_RELEASE_NOTES_AUTO", "false")
    monkeypatch.setenv("SCRIBE_RELEASE_NOTES_RATE_PER_S", "25")
    s = _settings()
    assert s.release_notes_auto is False
    assert s.release_notes_rate_per_s == 25


def test_open_bot_defaults_to_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #53: an open bot is an explicit choice; the default is off."""
    assert _settings().open_bot is False
    monkeypatch.setenv("SCRIBE_OPEN_BOT", "true")
    assert _settings().open_bot is True


def test_jira_allowed_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #61: 443 and 8443 by default; the env value is a comma-separated list."""
    assert _settings().jira_allowed_ports == [443, 8443]
    monkeypatch.setenv("SCRIBE_JIRA_ALLOWED_PORTS", " 443, 9443 ,443")
    assert _settings().jira_allowed_ports == [443, 9443]


@pytest.mark.parametrize("value", ["", "443,abc", "0", "65536", "443,-1"])
def test_jira_allowed_ports_rejects(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SCRIBE_JIRA_ALLOWED_PORTS", value)
    with pytest.raises(ValidationError):
        _settings()


# --- encryption keys (issues #58, #59) ----------------------------------------------------------

OLD_KEY = b"\x01" * 32
OLD_KEY_B64 = base64.b64encode(OLD_KEY).decode()


def test_key_defaults_keep_existing_data_readable() -> None:
    s = _settings()
    assert s.master_key_id == "v1"  # the id every value stored before #58 carries
    assert s.previous_master_keys == {}
    assert s.encryption_strict is False


def test_previous_keys_are_parsed() -> None:
    s = _settings(
        master_key_id="v2",
        master_keys_previous=f" v1:{OLD_KEY_B64} , old-0:{KEY_B64},",
    )
    assert s.previous_master_keys == {"v1": OLD_KEY, "old-0": b"\x00" * 32}
    assert OLD_KEY_B64 not in repr(s)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"master_keys_previous": OLD_KEY_B64}, "entry 1 must look like"),
        ({"master_keys_previous": f"v0:{OLD_KEY_B64},v0:{OLD_KEY_B64}"}, "listed twice"),
        ({"master_keys_previous": "v0:not-base64!"}, "entry 1 must be valid base64"),
        ({"master_keys_previous": f"v0:{base64.b64encode(b'x' * 16).decode()}"}, "32 bytes"),
        ({"master_keys_previous": f"bad id:{OLD_KEY_B64}"}, "entry 1 key id"),
        ({"master_keys_previous": f"v1:{OLD_KEY_B64}"}, "must not list master_key_id"),
        ({"master_key_id": "e2"}, "master_key_id"),
        ({"master_key_id": "v:2"}, "master_key_id"),
        ({"master_key": "!!!not-base64"}, "master_key must be valid base64"),
    ],
)
def test_key_errors_name_the_field_never_the_value(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _settings(**overrides)
    text = str(excinfo.value)
    assert message in text
    assert OLD_KEY_B64 not in text and "not-base64" not in text


def test_strict_mode_setting() -> None:
    assert _settings(encryption_strict=True).encryption_strict is True


def test_keyring_from_settings_holds_current_and_previous_keys() -> None:
    from scribe.crypto import Keyring, keyring_from_settings

    old_token = Keyring({"v1": OLD_KEY}, current="v1").encrypt("synthetic", "messages:text")
    ring = keyring_from_settings(
        _settings(master_key_id="v2", master_keys_previous=f"v1:{OLD_KEY_B64}")
    )
    assert ring.current == "v2" and ring.key_ids == {"v1", "v2"}
    assert ring.decrypt(old_token, "messages:text") == "synthetic"
    assert ring.encrypt("synthetic").startswith("e2:v2:")
    assert keyring_from_settings(_settings(encryption_strict=True)).strict is True
