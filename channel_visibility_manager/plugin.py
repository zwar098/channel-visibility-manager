"""
Channel Visibility Manager for Dispatcharr.

Marks certain channels as "static" (e.g. backup/placeholder feeds). For each
channel group that contains at least one static channel, the plugin checks
whether the group also contains any "dynamic" channel (any channel in the
group that isn't in the static list). Static channels in that group are then
shown (enabled) for the configured Dispatcharr Channel Profiles when a
dynamic channel is present, and hidden (disabled) when it isn't.

See README.md for setup and configuration.
"""

import logging
import threading
from datetime import datetime, timezone as dt_timezone

logger = logging.getLogger("plugins.channel_visibility_manager")

PLUGIN_KEY = "channel_visibility_manager"
POLL_INTERVAL_SECONDS = 20
LOCK_TIMEOUT_SECONDS = 90

_thread_lock = threading.Lock()
_worker_thread = None
_stop_event = threading.Event()


# ---------------------------------------------------------------------------
# Minimal dependency-free 5-field cron matcher (minute hour dom month dow)
# ---------------------------------------------------------------------------

def _cron_field_matches(field: str, value: int) -> bool:
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        if part == "*":
            return True
        if part.startswith("*/"):
            step = int(part[2:])
            if step > 0 and value % step == 0:
                return True
        elif "-" in part:
            lo, hi = part.split("-", 1)
            if int(lo) <= value <= int(hi):
                return True
        else:
            if int(part) == value:
                return True
    return False


def _cron_matches(expr: str, when: datetime) -> bool:
    fields = expr.strip().split()
    if len(fields) != 5:
        logger.warning("channel_visibility_manager: invalid cron expression %r", expr)
        return False
    minute, hour, dom, month, dow = fields
    # Python isoweekday(): Mon=1..Sun=7 -> cron dow: Sun=0..Sat=6
    cron_dow = when.isoweekday() % 7
    try:
        return (
            _cron_field_matches(minute, when.minute)
            and _cron_field_matches(hour, when.hour)
            and _cron_field_matches(dom, when.day)
            and _cron_field_matches(month, when.month)
            and _cron_field_matches(dow, cron_dow)
        )
    except ValueError:
        logger.warning("channel_visibility_manager: invalid cron expression %r", expr)
        return False


# ---------------------------------------------------------------------------
# Settings persistence helpers (PluginConfig.settings is the only storage
# plugins get, so internal scheduler state is stashed in there alongside the
# user-editable fields).
# ---------------------------------------------------------------------------

def _get_settings_dict():
    from apps.plugins.models import PluginConfig
    from django.db import close_old_connections

    try:
        cfg = PluginConfig.objects.filter(key=PLUGIN_KEY).first()
        return dict(cfg.settings or {}) if cfg else {}
    finally:
        close_old_connections()


def _persist_internal_state(**updates):
    from apps.plugins.models import PluginConfig
    from django.db import close_old_connections, transaction

    try:
        with transaction.atomic():
            cfg = PluginConfig.objects.select_for_update().filter(key=PLUGIN_KEY).first()
            if not cfg:
                return
            settings = dict(cfg.settings or {})
            settings.update(updates)
            cfg.settings = settings
            cfg.save(update_fields=["settings"])
    finally:
        close_old_connections()


def _parse_csv(value):
    return [v.strip() for v in (value or "").split(",") if v.strip()]


# ---------------------------------------------------------------------------
# Core enforcement logic
# ---------------------------------------------------------------------------

def _run_scan(settings, dry_run_override=None):
    from apps.channels.models import Channel, ChannelProfile, ChannelProfileMembership
    from django.db import close_old_connections

    static_names = _parse_csv(settings.get("static_channel_names"))
    profile_names = _parse_csv(settings.get("profile_names"))
    dry_run = settings.get("dry_run", False) if dry_run_override is None else dry_run_override

    if not static_names:
        return {"status": "error", "message": "No static channel names configured."}
    if not profile_names:
        return {"status": "error", "message": "No channel profile names configured."}

    report = []
    try:
        static_channels = list(
            Channel.objects.filter(name__in=static_names).select_related("channel_group")
        )
        matched_names = {c.name for c in static_channels}
        for n in static_names:
            if n not in matched_names:
                report.append(f"WARNING: no channel named '{n}' found; skipped.")

        groups = {}
        for ch in static_channels:
            if ch.channel_group_id is None:
                report.append(f"WARNING: '{ch.name}' has no channel group; skipped.")
                continue
            bucket = groups.setdefault(
                ch.channel_group_id, {"group": ch.channel_group, "static_ids": []}
            )
            bucket["static_ids"].append(ch.id)

        profiles = list(ChannelProfile.objects.filter(name__in=profile_names))
        matched_profiles = {p.name for p in profiles}
        for n in profile_names:
            if n not in matched_profiles:
                report.append(f"WARNING: no channel profile named '{n}' found; skipped.")

        if not groups or not profiles:
            report.append("Nothing to do (no matched groups or profiles).")
            return {"status": "ok", "message": "\n".join(report), "changes": 0}

        changes = 0
        for group_id, info in groups.items():
            static_ids = info["static_ids"]
            has_dynamic = (
                Channel.objects.filter(channel_group_id=group_id)
                .exclude(id__in=static_ids)
                .exists()
            )
            verb = "show" if has_dynamic else "hide"
            report.append(
                f"Group '{info['group'].name}': dynamic present={has_dynamic} -> "
                f"{verb} {len(static_ids)} static channel(s) across {len(profiles)} profile(s)"
            )
            if dry_run:
                continue
            for profile in profiles:
                updated = ChannelProfileMembership.objects.filter(
                    channel_profile=profile, channel_id__in=static_ids
                ).update(enabled=has_dynamic)
                changes += updated

        message = "\n".join(report)
        if dry_run:
            message = "[DRY RUN] " + message
        return {"status": "ok", "message": message, "changes": changes}
    finally:
        close_old_connections()


# ---------------------------------------------------------------------------
# Background scheduler
#
# Dispatcharr does not import plugin code inside its Celery worker/beat
# processes (only the web process), so a django_celery_beat PeriodicTask
# pointed at a task defined in this file would never be registered. Instead,
# this runs its own lightweight poll loop inside whichever process(es) do
# import this plugin, and uses Django's cache (Redis-backed in Dispatcharr)
# as a cross-process lock so only one process acts on a given cron minute.
# ---------------------------------------------------------------------------

def _tick():
    from django.core.cache import cache

    cfg_settings = _get_settings_dict()
    if not cfg_settings.get("_schedule_enabled"):
        return

    cron_expr = (cfg_settings.get("cron_schedule") or "").strip()
    if not cron_expr:
        return

    now = datetime.now(dt_timezone.utc).replace(second=0, microsecond=0)
    if not _cron_matches(cron_expr, now):
        return

    lock_key = f"channel_visibility_manager:tick:{now.isoformat()}"
    if not cache.add(lock_key, "1", timeout=LOCK_TIMEOUT_SECONDS):
        return  # another process already claimed this minute

    result = _run_scan(cfg_settings)
    _persist_internal_state(
        _last_run_at=now.isoformat(),
        _last_run_result=(result.get("message", "") or "")[:4000],
    )
    logger.info("channel_visibility_manager: scheduled scan result: %s", result.get("message"))


def _worker_loop():
    while not _stop_event.is_set():
        try:
            _tick()
        except Exception:
            logger.exception("channel_visibility_manager: scheduler tick failed")
        _stop_event.wait(POLL_INTERVAL_SECONDS)


def _ensure_worker_started():
    global _worker_thread
    with _thread_lock:
        if _worker_thread is None or not _worker_thread.is_alive():
            _stop_event.clear()
            _worker_thread = threading.Thread(
                target=_worker_loop,
                name="channel-visibility-manager-scheduler",
                daemon=True,
            )
            _worker_thread.start()


# Start the poll loop as soon as Dispatcharr imports this module. It stays
# idle (one settings read every POLL_INTERVAL_SECONDS) until a user clicks
# "Enable Schedule", and picks that flag up automatically after a restart.
_ensure_worker_started()


# ---------------------------------------------------------------------------
# Plugin interface
# ---------------------------------------------------------------------------

class Plugin:
    name = "Channel Visibility Manager"
    version = "0.0.1"
    description = (
        "Hides 'static' channels in a channel group for chosen profiles when no "
        "dynamic channels are present in that group, and shows them again once "
        "a dynamic channel appears."
    )
    author = "zwar098"
    help_url = "https://github.com/zwar098/channel-visibility-manager"

    fields = [
        {
            "id": "static_channel_names",
            "label": "Static channel names",
            "type": "string",
            "default": "",
            "help_text": (
                "Comma-separated exact channel names to treat as static, e.g. "
                "'Backup Feed 1, NFL Redzone Backup'. Only channel groups that "
                "contain at least one of these are scanned."
            ),
        },
        {
            "id": "profile_names",
            "label": "Channel profile names",
            "type": "string",
            "default": "",
            "help_text": (
                "Comma-separated Dispatcharr Channel Profile names to enforce "
                "visibility on, e.g. 'Default, Kids'."
            ),
        },
        {
            "id": "cron_schedule",
            "label": "Cron schedule (UTC)",
            "type": "string",
            "default": "*/15 * * * *",
            "help_text": (
                "Standard 5-field cron expression (minute hour day month weekday), "
                "evaluated in UTC. Editing this does NOT move a running schedule - "
                "click 'Enable Schedule' again to apply changes."
            ),
        },
        {
            "id": "dry_run",
            "label": "Dry run",
            "type": "boolean",
            "default": False,
            "help_text": (
                "When on, 'Scan Now' and the schedule log what they WOULD change "
                "without changing anything."
            ),
        },
    ]

    actions = [
        {
            "id": "scan_now",
            "label": "Scan Now",
            "description": "Run the visibility scan immediately using the current settings.",
        },
        {
            "id": "preview",
            "label": "Preview (dry run)",
            "description": "Report what the scan would change, without changing anything.",
        },
        {
            "id": "enable_schedule",
            "label": "Enable Schedule",
            "description": "Start running the scan automatically on the configured cron schedule.",
        },
        {
            "id": "disable_schedule",
            "label": "Disable Schedule",
            "description": "Stop the automatic schedule. Scan Now/Preview still work manually.",
        },
        {
            "id": "schedule_status",
            "label": "Schedule Status",
            "description": "Show whether the schedule is enabled and when it last ran.",
        },
    ]

    def run(self, action: str, params: dict, context: dict):
        settings = context.get("settings", {}) or {}

        if action == "scan_now":
            return _run_scan(settings)

        if action == "preview":
            return _run_scan(settings, dry_run_override=True)

        if action == "enable_schedule":
            cron_expr = (settings.get("cron_schedule") or "").strip()
            if len(cron_expr.split()) != 5:
                return {
                    "status": "error",
                    "message": "Set a valid 5-field cron_schedule before enabling.",
                }
            _persist_internal_state(_schedule_enabled=True)
            _ensure_worker_started()
            return {
                "status": "ok",
                "message": (
                    f"Schedule enabled: '{cron_expr}' (UTC). "
                    f"Applies within {POLL_INTERVAL_SECONDS}s."
                ),
            }

        if action == "disable_schedule":
            _persist_internal_state(_schedule_enabled=False)
            return {"status": "ok", "message": "Schedule disabled."}

        if action == "schedule_status":
            cfg_settings = _get_settings_dict()
            enabled = bool(cfg_settings.get("_schedule_enabled"))
            cron_expr = cfg_settings.get("cron_schedule") or "(none)"
            last_run = cfg_settings.get("_last_run_at") or "never"
            last_result = cfg_settings.get("_last_run_result") or ""
            return {
                "status": "ok",
                "message": (
                    f"Enabled: {enabled}\nCron: {cron_expr}\nLast run (UTC): {last_run}"
                    + (f"\n\n{last_result}" if last_result else "")
                ),
            }

        return {"status": "error", "message": f"Unknown action: {action}"}

    def stop(self, context: dict):
        _stop_event.set()
