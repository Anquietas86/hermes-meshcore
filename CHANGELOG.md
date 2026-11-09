# Changelog

## 2.1.0

### Security

- Public senders no longer get every MCP server and plugin toolset. Hermes
  adds all enabled MCP servers and plugin toolsets to a platform's tool
  list unless told otherwise, so a stranger on the mesh could reach, for
  example, a Home Assistant MCP server. The public list now carries
  Hermes's `no_mcp` marker, and a `pre_tool_call` hook blocks any tool
  outside the public allowlist in non-admin MeshCore sessions. The hook
  fails closed. Admins are unaffected.

### Added

- **Public access** card on the dashboard: every tool the running gateway
  has, grouped by toolset and marked read-only, can change things, or not
  marked. Tick the ones strangers may use.
- `MESHCORE_PUBLIC_TOOLS`: single extra tools for non-admin senders, which
  the card writes.
- `GET /api/plugins/meshcore-platform/tools` for the card.

## 2.0.3

### Changed

- Strangers get help with general information again. 2.0.0's prompt was so
  strict that the model deflected ordinary questions. It now tells the
  model to help anyone with weather, the mesh, propagation, news and general
  knowledge, and to keep only two things off limits: the operator's
  personal information, and changing anything on the operator's behalf.
- Public senders get the `web` toolset (read-only search and extract) as
  well as `meshcore` by default.
- Admin channels get everything: the prompt no longer tells the model to
  keep memory and the user profile from their members. It still doesn't
  assume the sender is the operator, since channel names are unverified.

### Added

- "Tools for everyone else" (`MESHCORE_PUBLIC_TOOLSETS`) on the dashboard.

## 2.0.2

### Fixed

- Messages are no longer refused by Hermes with "Hermes refused this
  message (invalid_params)". Current Hermes rejects inbound events that
  carry platform metadata it doesn't know, and the adapter attached radio
  details there. They were already in the per-message prompt, so the
  metadata is gone.
- The dashboard's contact list and contact picker reload when the gateway
  reconnects, instead of staying on "Gateway not connected" after a
  restart.
- A stray "0" no longer appears under the home channel field.

### Added

- The admin node and allowed sender fields warn about keys that aren't in
  the node's contacts.

## 2.0.1

### Fixed

- The dashboard tab finds the profile that runs MeshCore. When the
  dashboard's own profile has no MeshCore gateway (for example, the bot
  runs under `hermes -p meshcore` and the dashboard on the default
  profile), it uses the profile whose gateway reported most recently. 2.0.0
  only looked at the dashboard's own profile, so it showed "Disconnected"
  while the bot was running fine. Settings, channel changes and queries
  follow the same profile. The status card shows which one it is.

### Added

- Node connection settings on the dashboard: host and port, plus a
  "Test connection" button. The test only runs while the gateway isn't
  connected, because the node serves one client at a time.
- Home channel and repeater-watch alert target on the dashboard, with a
  contact picker and a warning when the key isn't one of the node's
  contacts.
- When the gateway can't connect, the dashboard shows why: for example,
  refused, timed out, or connected but the node never answered (usually
  the MeshCore app or an old gateway process holding the link).

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
- Disconnecting no longer hangs on Python 3.11, where `asyncio.wait_for`
  could swallow a cancellation and stall the gateway's reconnect.

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
- Every message tells the model who sent it, and whether that sender is the
  operator (verified by radio key). This stops the model treating everyone
  on a public channel as the operator, using the operator's name, or
  repeating their memory and profile. The README covers the stronger fix: a
  separate profile with memory turned off.
- Multi-part replies no longer lose parts:
  - Parts are paced by LoRa airtime instead of a fixed 1 second, so the next
    part isn't sent while repeaters are still relaying the last one.
  - Replies are sent one at a time, never interleaved.
  - DM parts wait for the recipient's ACK and are retried once over a flood
    route if none arrives.
- Only final answers go over the air. The platform declares Hermes's
  "minimal" display tier, so there are no tool-progress, streaming, interim,
  reasoning, long-running or busy-ack messages.
- A per-sender rate limit (`MESHCORE_RATE_LIMIT`, default 6 per 10 minutes).
  Admins are exempt.
- Recent channel messages go along with a mention as context
  (`MESHCORE_CHANNEL_CONTEXT`).
- `meshcore_nearby` tool: the nearest repeaters or nodes, by advert position.
- Repeater watch (`MESHCORE_WATCH_*`): DMs you when a watched repeater stops
  advertising or reports a low battery, and when it recovers.
- Add and remove channels on the node from the dashboard. Hashtag channels
  derive their key from the name; private channels take a key or get a
  random one.
- Experimental USB serial support (`MESHCORE_HOST=serial:/dev/ttyUSB0`).
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
