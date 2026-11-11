"""Application settings loaded from environment variables (prefix ``SCRIBE_``).

Secrets are typed as ``SecretStr`` so they never appear in ``repr()`` or logs, and validation
errors never echo an input (``hide_input_in_errors``): they name the field only.
LLM route-table loading and ``SCRIBE_LLM__*`` overrides live in ``scribe.llm.routes``.
"""

from __future__ import annotations

import base64
import binascii
import importlib.metadata
import json
import re
from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

MASTER_KEY_BYTES = 32
# Same rule as crypto.valid_key_id (config never imports crypto); "e2" is a format marker.
_KEY_ID = re.compile(r"[A-Za-z0-9_.-]{1,32}")
_RESERVED_KEY_IDS = frozenset({"e2"})


def _decode_key(encoded: str, field: str) -> bytes:
    """A base64 AES-256 key; errors name ``field`` only, never the value."""
    try:
        raw = base64.b64decode(encoded.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{field} must be valid base64") from exc
    if len(raw) != MASTER_KEY_BYTES:
        raise ValueError(f"{field} must decode to {MASTER_KEY_BYTES} bytes")
    return raw


def _check_key_id(key_id: str, field: str) -> str:
    if not _KEY_ID.fullmatch(key_id) or key_id in _RESERVED_KEY_IDS:
        raise ValueError(f"{field} must be 1-32 of A-Z a-z 0-9 _ . - (and not 'e2')")
    return key_id


def parse_previous_master_keys(value: str) -> dict[str, bytes]:
    """``SCRIBE_MASTER_KEYS_PREVIOUS``: comma-separated ``<key id>:<base64 key>`` entries."""
    keys: dict[str, bytes] = {}
    for position, entry in enumerate((e.strip() for e in value.split(",")), start=1):
        if not entry:
            continue
        field = f"master_keys_previous entry {position}"
        key_id, sep, encoded = entry.partition(":")
        if not sep:
            raise ValueError(f"{field} must look like <key id>:<base64 key>")
        key_id = _check_key_id(key_id.strip(), f"{field} key id")
        if key_id in keys:
            raise ValueError(f"{field}: key id {key_id!r} is listed twice")
        keys[key_id] = _decode_key(encoded, field)
    return keys


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SCRIBE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,  # inputs may be keys or tokens
    )

    # --- required ---------------------------------------------------------------------------
    database_url: SecretStr
    master_key: SecretStr
    bot_token: SecretStr
    tg_api_id: int
    tg_api_hash: SecretStr

    # --- encryption keys (issues #58, #59; docs/configuration.md "Rotating the master key") --
    # Id of SCRIBE_MASTER_KEY, written into every new ciphertext. "v1" is the id every value
    # stored so far carries: keep it until the first rotation.
    master_key_id: str = "v1"
    # Keys that only decrypt, after a rotation: "<key id>:<base64 key>,…".
    master_keys_previous: SecretStr = SecretStr("")
    # Refuse plaintext, legacy-format ciphertexts and unsealed session strings (turn on once
    # `python -m scribe.jobs reencrypt_all --check` reports nothing pending).
    encryption_strict: bool = False

    # --- runtime ----------------------------------------------------------------------------
    scheduler_tick_s: float = Field(default=5, gt=0)
    read_concurrency: int = Field(default=50, ge=1)  # parallel mark-read calls (SC-009)
    compose_concurrency: int = Field(default=10, ge=1)  # parallel draft compositions
    draft_remind_after_s: int = Field(default=7200, gt=0)  # FR-032: one reminder after 2 h
    draft_expire_after_s: int = Field(default=86400, gt=0)  # FR-032: expiry after 24 h
    # Issue #73: 🔁 Regenerate at most this many times per draft on the same messages.
    regenerate_max_per_draft: int = Field(default=3, ge=1)
    log_level: str = "INFO"
    # Issue #55: let third-party SDK loggers (anthropic, openai, telethon, aiogram) follow
    # log_level below INFO. Their DEBUG output contains message content: troubleshooting only.
    log_libraries_debug: bool = False
    # "0.0.0.0" suits the container (compose publishes it on 127.0.0.1 only); on bare metal or
    # Kubernetes set 127.0.0.1 or keep the port private: /healthz has no authentication.
    health_host: str = "0.0.0.0"
    health_port: int = Field(default=8080, ge=0, le=65535)  # 0 = any free port (tests)
    test_fast_clock: bool = False

    # --- files (relative to the working directory; the image runs from /app) ---------------
    llm_routes_path: Path = Path("config/llm_routes.yml")
    llm_prices_path: Path = Path("config/llm_prices.yml")
    prompts_dir: Path = Path("prompts")

    # --- per-user defaults (copied into new users; editable per user) ------------------------
    delay_min_s: int = Field(default=120, ge=0, le=86400)
    delay_max_s: int = Field(default=600, ge=0, le=86400)
    work_start: time = time(9, 0)
    work_end: time = time(19, 0)
    working_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    grouping_window_s: int = Field(default=60, ge=0)

    # --- limits -----------------------------------------------------------------------------
    max_contact_profiles: int = Field(default=20, ge=0)
    message_retention_days: int = Field(default=30, ge=1)
    # Issue #67: daily per-user LLM spend rows (``llm_spend``) older than this are purged.
    llm_spend_retention_days: int = Field(default=90, ge=1)
    # Issue #74: per-user LLM budget per UTC day (the ``llm_spend`` day), in USD; 0 disables a
    # cap. At the soft cap drafts use the economy tier; at the hard cap drafting pauses until
    # the next UTC day (docs/configuration.md "Daily LLM budget per user").
    llm_daily_soft_cap_usd: float = Field(default=1.0, ge=0)
    llm_daily_hard_cap_usd: float = Field(default=3.0, ge=0)
    style_import_months: int = Field(default=6, ge=1)
    profile_examples_contact: int = Field(default=20, ge=0)
    profile_examples_global: int = Field(default=50, ge=0)
    min_style_samples: int = Field(default=50, ge=0)
    # Style profile builds (issue #30): own messages sent to the model per profile (lower it for
    # a smaller model), and how much of the global profile's sample group chats may take.
    profile_max_samples: int = Field(default=400, ge=1)
    # Automatic profile refresh (job ``refresh_profiles``, Sundays): a user is refreshed at most
    # once per interval, a profile only from this many own messages written since it was built,
    # and only while the user's /settings switch is on (``users.profile_auto_refresh``; NULL
    # follows the default below).
    profile_refresh_interval_days: int = Field(default=7, ge=1)
    profile_refresh_min_new_messages: int = Field(default=10, ge=1)
    profile_auto_refresh_default: bool = True
    profile_group_samples_per_chat: int = Field(default=40, ge=0)
    profile_group_samples_share: float = Field(default=0.3, ge=0, le=1)
    # Work account mode (specs/008 FR-016): the most recent private chats the style import reads
    # on top of the enabled work chats while the mode is on.
    work_account_import_max_chats: int = Field(default=50, ge=0)

    # --- reply gate: skip messages that need no reply (specs/004-skip-closing-messages) -----
    skip_candidate_max_chars: int = Field(default=2000, ge=1)  # pre-gate length limit (013)
    skip_min_confidence: float = Field(default=0.8, ge=0, le=1)  # classifier confidence to skip
    skip_context_messages: int = Field(default=8, ge=0)  # history messages for the classifier
    skip_habit_window: int = Field(default=5, ge=1)  # past closing turns examined (research R3)
    skip_habit_min_replies: int = Field(default=3, ge=1)  # replies among them that force a draft
    skip_habit_reply_window_min: int = Field(default=30, ge=1)  # outgoing within = a reply
    skipped_list_days: int = Field(default=7, ge=1)  # /skipped look-back

    # --- Telegram login (specs/002-telegram-qr-login) -----------------------------------------
    tg_qr_max_codes: int = Field(default=5, ge=1)
    tg_login_password_timeout_s: int = Field(default=600, gt=0)
    tg_qr_same_device_link: bool = False  # off until verified on real apps (research Q5)
    tg_device_model: str = "Scribe Agent"  # shown in Telegram's "Active sessions"
    tg_system_version: str = "server"
    tg_app_version: str = Field(default_factory=lambda: importlib.metadata.version("scribe"))

    # --- control bot ------------------------------------------------------------------------
    # ✏️ Edit of a short draft: "inline" fills the input field (needs inline mode in BotFather),
    # "copy" copies the draft; "auto" picks "inline" when getMe says inline mode is on.
    edit_prefill: Literal["auto", "inline", "copy"] = "auto"
    # --- Jira task source (specs/003-jira-commitments) ----------------------------------------
    jira_dc_min_version: str = "10.3"  # oldest self-hosted Jira accepted (research J6)
    # Hosts or CIDRs a self-hosted Jira may resolve to although they are private (research J4).
    jira_allowed_private_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Ports a new Jira address may use (issue #61). Env: comma-separated, e.g. "443,8443,9443";
    # the full list, so keep 443 in it if you still want the standard port.
    jira_allowed_ports: Annotated[list[int], NoDecode] = Field(default_factory=lambda: [443, 8443])
    jira_sync_interval_s: int = Field(default=300, ge=60)  # comment sync cadence (FR-J13)
    jira_sync_concurrency: int = Field(default=5, ge=1)
    jira_lookback_days: int = Field(default=30, ge=1)  # FR-J07
    jira_first_scan_max_issues: int = Field(default=1000, ge=1)
    # --- search over earlier conversations (specs/004-conversation-search) ---------------------
    earlier_search_enabled: bool = True  # operator kill switch (research S9)
    earlier_search_timeout_s: float = Field(default=6, ge=1, le=9)  # search step deadline (S4)

    # --- access gate (specs/005-beta-subscription-access) -------------------------------------
    access_gate: bool = False  # off: every sender is served as before (FR-021)
    # Issue #53: with the gate off, `python -m scribe` refuses to start unless this is true:
    # an open bot (anyone who finds it can log in and spend the LLM key) is an explicit choice.
    open_bot: bool = False
    # Always have access (FR-022). Env: comma-separated ids, e.g. "900000001,900000002".
    operator_telegram_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    # Shown with the subscription placeholder and in renewal reminders (research Q14); empty:
    # left out. People ask for beta access inside the bot (US5).
    subscription_contact: str = ""
    access_refusal_cooldown_s: int = Field(default=3600, gt=0)  # FR-006, research Q5

    # --- release notes (specs/011-release-notes) -----------------------------------------------
    release_notes_dir: Path = Path("release-notes")  # relative to /app in the image
    release_notes_auto: bool = True  # announce a new version at startup (FR-008)
    release_notes_rate_per_s: float = Field(default=10, ge=1, le=25)  # overall pace (FR-013)
    release_notes_max_attempts: int = Field(default=5, ge=1)  # retryable errors before failed

    @field_validator("master_key")
    @classmethod
    def _master_key_is_32_bytes(cls, value: SecretStr) -> SecretStr:
        _decode_key(value.get_secret_value(), "master_key")
        return value

    @field_validator("master_key_id")
    @classmethod
    def _master_key_id_valid(cls, value: str) -> str:
        return _check_key_id(value.strip(), "master_key_id")

    @field_validator("master_keys_previous")
    @classmethod
    def _previous_keys_valid(cls, value: SecretStr) -> SecretStr:
        parse_previous_master_keys(value.get_secret_value())
        return value

    @field_validator("working_days")
    @classmethod
    def _working_days_valid(cls, value: list[int]) -> list[int]:
        if not value:
            raise ValueError("working_days must not be empty")
        if any(d < 0 or d > 6 for d in value):
            raise ValueError("working_days values must be 0 (Mon) .. 6 (Sun)")
        return sorted(set(value))

    @field_validator("operator_telegram_ids", mode="before")
    @classmethod
    def _split_operator_ids(cls, value: Any) -> Any:
        """Accept a comma-separated env value (or a JSON list) besides a real list."""
        if not isinstance(value, str):
            return value
        value = value.strip()
        if value.startswith("["):
            return json.loads(value)
        return [part.strip() for part in value.split(",") if part.strip()]

    @field_validator("jira_allowed_private_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("jira_allowed_ports", mode="before")
    @classmethod
    def _split_ports(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("jira_allowed_ports")
    @classmethod
    def _ports_valid(cls, value: list[int]) -> list[int]:
        if not value:
            raise ValueError("jira_allowed_ports must list at least one port")
        if any(p < 1 or p > 65535 for p in value):
            raise ValueError("jira_allowed_ports values must be 1 .. 65535")
        return list(dict.fromkeys(value))

    @field_validator("jira_dc_min_version")
    @classmethod
    def _jira_version(cls, value: str) -> str:
        parts = value.strip().split(".")
        if not parts or not all(p.isdigit() for p in parts):
            raise ValueError("jira_dc_min_version must look like 10.3")
        if tuple(int(p) for p in parts) < (8, 14):
            raise ValueError("jira_dc_min_version must be 8.14 or newer (personal access tokens)")
        return value.strip()

    @field_validator("log_level")
    @classmethod
    def _log_level_upper(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"unknown log level {value!r}")
        return level

    @model_validator(mode="after")
    def _ranges_consistent(self) -> Self:
        if self.delay_min_s > self.delay_max_s:
            raise ValueError("delay_min_s must be <= delay_max_s")
        if self.work_start >= self.work_end:
            raise ValueError("work_start must be earlier than work_end")
        soft, hard = self.llm_daily_soft_cap_usd, self.llm_daily_hard_cap_usd
        if soft > 0 and hard > 0 and soft > hard:
            raise ValueError("llm_daily_soft_cap_usd must be <= llm_daily_hard_cap_usd")
        if self.master_key_id in self.previous_master_keys:
            raise ValueError("master_keys_previous must not list master_key_id (the current key)")
        return self

    @property
    def master_key_bytes(self) -> bytes:
        return base64.b64decode(self.master_key.get_secret_value())

    @property
    def previous_master_keys(self) -> dict[str, bytes]:
        """Decryption-only keys by id (validated at load)."""
        return parse_previous_master_keys(self.master_keys_previous.get_secret_value())

    @property
    def review_deadlines_s(self) -> tuple[int, int]:
        """Seconds from a draft's preparation to its (reminder, expiry); 60 / 120 with
        ``test_fast_clock`` (quickstart US2) so the flow can be checked by hand."""
        if self.test_fast_clock:
            return 60, 120
        return self.draft_remind_after_s, self.draft_expire_after_s


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, read once from the environment."""
    return Settings()  # required fields come from the environment
