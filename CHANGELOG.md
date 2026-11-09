# Changelog

## 2.0.0

A ground-up rebuild against the current Hermes platform plugin contract.

### Fixed

- Reconnects no longer cancel themselves. On link loss the adapter reports a
  retryable fatal error and lets the gateway's reconnect watcher rebuild it.
  The old silence watchdog only ever fired once.
- Remote queries return as soon as the repeater answers, instead of always
  waiting out the full timeout. Replies are matched by tag.
- DMs and channel messages that arrive during a remote query are no longer
  swallowed.
- DM text containing a colon is no longer cut at the colon.
- Repeater CLI replies (`txt_type` 1) no longer reach the agent as chat.
- The `all` command works, and logs in once.
- Commands that silently returned the status block (`ver`, `board`,
  `get …`) have been removed.
- Tools and the dashboard no longer pick up another profile's adapter or
  state.
- The `meshcore_contact` and `meshcore_admin` tools now exist in CLI and TUI
  sessions (`provides_tools`).
- The dashboard backend imports when Hermes loads it standalone. Config
  writes are validated (no `.env` injection), and the restart button no
  longer blocks the dashboard.
- Replies are split once, by bytes, with room for the node name on channels.

### Added

- Gateway-side allowlist integration: `MESHCORE_GROUP_ALLOWED_CHATS`,
  resolution of short pubkey prefixes, and pairing codes never broadcast
  when an allowlist is set.
- Cron and `hermes send` delivery through the running gateway (standalone
  sender).
- A scoped lock, so two profiles cannot fight over one node.
- `MESHCORE_PUBLIC_TOOLSETS`, `MESHCORE_ADMIN_TOOLSETS`,
  `MESHCORE_MAX_MESSAGE_BYTES`, `MESHCORE_MAX_CHUNKS` and
  `MESHCORE_ADVERT_ON_START`.
- `@[Name]` mentions, as sent by the MeshCore app.
- New remote commands: `telemetry`, `acl` and `regions`, with parsed output.
- A new test suite with a fake companion node over TCP, and CI that runs it.

### Breaking changes and migration

- `channel:N` entries in `MESHCORE_ALLOWED_USERS` are no longer honoured by
  the gateway's allowlist. Move them to `MESHCORE_GROUP_ALLOWED_CHATS`. A
  warning is logged until you do.
- Admins now get the profile's own `platform_toolsets.meshcore` instead of a
  hard-coded list. Set `MESHCORE_ADMIN_TOOLSETS` to pin one.
- Public senders can no longer run gateway slash commands.
- `meshcore_admin_query` is merged into `meshcore_admin`. The command names
  are now `status`, `telemetry`, `neighbours`, `acl`, `owner`, `clock`,
  `regions` and `all`. Old spellings that map to the same data
  (`neighbors`, `stats-core`, `req_status`, …) still work.
- `MESHCORE_ADMIN_NODES` entries shorter than 8 hex characters are ignored.
- The dashboard no longer accepts repeater passwords, because they would
  have to be written to disk. Use guest access, or ask the agent from an
  admin DM.
- The dashboard step is now `hermes plugins enable meshcore-platform`. The
  old `dashboard.trusted_plugin_apis` setting never existed in Hermes and can
  be removed.

## 1.11.0

- Support Hermes singular multiplexed gateways with a per-profile adapter registry.
- Remove the process-wide MeshCore adapter singleton that prevented multiple profiles.
- Resolve the active adapter by profile context for tool handlers.
- Derive profile-scoped IPC state from `HERMES_HOME` when `HERMES_PROFILE_DIR` is not explicitly set.
- Align the dashboard manifest version with the plugin release.

## 1.10.1

- Fix package-relative `meshcore_utils` imports when Hermes loads the plugin as a package.
- Retain a narrow top-level import fallback for the standalone test runner.
- Restore passwordless cross-process admin-query submission while keeping gateway-side authorization.
- Tighten parser boundary validation and add regression coverage for malformed frames.
- Fix dashboard radio-stat key mapping and CI Python executable resolution.

## 1.10.0

- Enforce source-scoped admin tool access with a dedicated `meshcore_admin` toolset.
- Reject arbitrary remote commands; only documented read-only commands are accepted.
- Replace global IPC files with profile-scoped, atomic `0600` files and request IDs.
- Reject password-bearing cross-process IPC; passwords remain in-memory only.
- Align dashboard configuration with the adapter's scoped environment keys.
- Harden packet parsing, frame bounds, field lengths, and count validation.
- Add focused security/regression tests and CI coverage.

## 1.9.7

- Previous release.
