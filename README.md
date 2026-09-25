# Channel Visibility Manager

A [Dispatcharr](https://github.com/dispatcharr/dispatcharr) plugin that keeps
"static" placeholder/backup channels hidden until they're actually needed.

Mark specific channels as **static**. For any channel group that contains at
least one static channel, the plugin checks whether the group also has a
**dynamic** channel (any other channel in that same group). If a dynamic
channel is present, the static channel(s) are shown (enabled) for the
Dispatcharr Channel Profiles you configure; if the group has been whittled
down to static-only (or the dynamic channels are gone), the static
channel(s) are hidden (disabled) for those profiles.

Typical use: an M3U source adds/removes event channels (e.g. PPV feeds) into
a group automatically. You keep a "backup"/"no event scheduled" channel
always defined in that group, but only want it visible to viewers while
there's actually a live event channel alongside it.

## How it works

- Visibility is enforced via Dispatcharr's own per-profile channel
  membership (`ChannelProfileMembership.enabled`) — nothing outside
  Dispatcharr's normal channel/profile model is touched.
- "Static" is not a Dispatcharr concept; this plugin tracks it purely by the
  channel names you list in settings.
- Only channel groups that contain at least one of your listed static
  channels are ever scanned or modified.
- Multiple static channels in the same group always get the same
  show/hide verdict for a given profile — they can't fight each other.

## Installation

1. Download `channel-visibility-manager-<version>.zip` from the
   [latest release](../../releases/latest).
2. Upload it via Dispatcharr's Plugins UI ("Import Plugin") — the zip's
   top-level entry is the `channel_visibility_manager/` folder itself, as
   Dispatcharr expects.
3. Enable the plugin from the Dispatcharr Plugins page.

## Settings

| Field | Description |
|---|---|
| **Static channel names** | Comma-separated, exact channel names to treat as static, e.g. `Backup Feed 1, NFL Redzone Backup`. |
| **Channel profile names** | Comma-separated Dispatcharr Channel Profile names to enforce visibility on, e.g. `Default, Kids`. |
| **Cron schedule** | Standard 5-field cron expression (`minute hour day month weekday`), evaluated in the configured timezone. |
| **Timezone** | IANA timezone name the cron schedule is evaluated in, e.g. `America/New_York`, `Europe/London`. Defaults to `UTC`. |
| **Dry run** | When on, scans report what they *would* change without changing anything. |

Channel names must match exactly. Unmatched names or profile names are
reported as warnings in the action result rather than failing the whole
scan.

## Actions

- **Scan Now** — run the enforcement logic immediately with current settings.
- **Preview (dry run)** — same, but reports intended changes without applying them.
- **Enable Schedule** — starts running the scan automatically on the
  configured cron schedule. Takes effect within ~20 seconds.
- **Disable Schedule** — stops the automatic schedule. Manual actions still work.
- **Schedule Status** — reports whether the schedule is enabled and when it
  last ran, along with the plugin key it's reading/writing state under (see
  below if `Enable Schedule` reports success but Status still shows disabled).

### About the schedule

Dispatcharr's Celery worker/beat processes don't import plugin code, so this
plugin can't rely on Dispatcharr's `django_celery_beat` integration to fire
its own scheduled task. Instead, once you click **Enable Schedule**, the
plugin runs its own lightweight poll loop inside the Dispatcharr web
process(es) (checking every ~20 seconds whether the cron expression matches
the current UTC minute), and uses Dispatcharr's shared Redis-backed cache as
a cross-process lock so a given cron minute is only acted on once even if
Dispatcharr is running multiple web workers.

Practical implications:
- **Editing** the cron expression, timezone, or saving settings does **not**
  move an already-running schedule — click **Enable Schedule** again to
  apply the change.
- The schedule's enabled/disabled state is stored in the plugin's own
  settings, so it survives a Dispatcharr restart and resumes automatically.
- Disabling, deleting, or reloading the plugin stops the poll loop
  immediately (via the plugin's `stop()` hook).
- The cross-process lock that prevents double-running is always keyed on
  the underlying UTC instant, so changing the timezone field can't cause a
  minute to be skipped or double-fired around a DST transition.
- Timezone names are resolved with Python's `zoneinfo`, which depends on a
  system (or `tzdata` package) timezone database. If your Dispatcharr
  container image doesn't have one, an unrecognized name falls back to UTC
  and logs a warning rather than failing the schedule.
- Scheduler state (enabled/disabled, last run) is stored under a plugin key
  derived from the installed folder name, not hardcoded, so it always
  matches whatever key Dispatcharr actually assigned on import. `Enable
  Schedule` now reports an error instead of a false success if that lookup
  ever fails; `Schedule Status` shows the key it's using for comparison.

## Building a release zip

Always build the release archive with `python scripts/build_release_zip.py`
rather than Windows' `Compress-Archive`. `Compress-Archive` writes backslash
path separators inside the zip, which isn't valid per the ZIP spec —
Dispatcharr's importer (running on Linux) doesn't treat `\` as a directory
separator, so it fails with "missing plugin.py or package `__init__.py`"
even though the file is right there.

## License

MIT — see [LICENSE](LICENSE).
