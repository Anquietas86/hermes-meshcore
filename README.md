# Hermes MeshCore Platform Adapter

Connects [Hermes Agent](https://github.com/NousResearch/hermes-agent) to a
[MeshCore](https://meshcore.net/) companion radio node via TCP, enabling
LoRa mesh communication as a native messaging platform.

Uses the **raw binary protocol** directly — zero external dependencies, Python
standard library only.

## Features

- **Channel monitoring** — respond in mesh channels with @mention detection
- **Direct messages** — private conversations with fire-and-forget delivery
- **Node-ID authorization** — admin nodes get full access, public users are restricted
- **Message splitting** — auto-splits long responses at word boundaries with (N/M) markers
- **Security-aware** — warns the model not to leak credentials in public broadcasts
- **Admin channels** — mark trusted channels for sensitive replies
- **Radio metadata** — RSSI, SNR, hops, and path info injected into channel context
- **Self-healing** — silence watchdog with automatic reconnect, stale frame drain
- **Remote repeater admin** — query remote repeaters via CLI commands over the mesh (`meshcore_admin` tool)
- **Contact lookup** — instant node details from the contact cache (`meshcore_contact` tool)
- **Password auth** — admin password support for repeaters that require authentication

## Quick Install

```bash
hermes plugins install Anquietas86/hermes-meshcore
```

Or from the dashboard: Plugins → Install → enter `Anquietas86/hermes-meshcore`.

## Configuration

Add to `~/.hermes/.env`:

```bash
# Required
MESHCORE_HOST=192.168.0.141
MESHCORE_PORT=5000

# Recommended
MESHCORE_ADMIN_NODES=your-pubkey-prefix     # Your node's pubkey prefix
MESHCORE_HOME_CHANNEL=dm:your-pubkey-prefix # Where cron/notifications go
MESHCORE_MONITOR_CHANNELS=1           # Channels to respond in (empty = discover only)
MESHCORE_ENABLE_DMS=true
MESHCORE_ALLOWED_USERS=your-pubkey-prefix,channel:1  # Who can talk (DMs by pubkey, channels by index)

# Optional
MESHCORE_ADMIN_CHANNELS=1             # Channels trusted for sensitive replies
MESHCORE_REQUIRE_MENTION=0,2,3        # Channels requiring @mention (empty = none, "true" = all)
MESHCORE_DEBUG=false                  # Enable packet-level debug logging (default: false)
```

### Per-channel @mention gating

`MESHCORE_REQUIRE_MENTION` accepts three formats:

| Value | Behavior |
|-------|----------|
| `true` / `1` / `yes` | All monitored channels require @mention |
| `0,2,3` | Only channels 0, 2, 3 require @mention (channel 1 is free-for-all) |
| *(empty)* | No channels require @mention — respond to everything |

The bot name for @mention detection is auto-derived from your node's advert
name. `MESHCORE_BOT_NAME` is available as an override if needed.

Then enable in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - meshcore-platform
```

Restart the gateway:

```bash
hermes gateway restart
```

## Authorization Model

DMs and channels use different identity models:

| | DM | Channel |
|---|---|---|
| **Identity** | `pubkey_prefix` (cryptographic) | `channel:<idx>` (stable synthetic) |
| **Admin check** | `MESHCORE_ADMIN_NODES` | `MESHCORE_ADMIN_CHANNELS` |
| **Allowlist** | `MESHCORE_ALLOWED_USERS=your-pubkey-prefix` | `MESHCORE_ALLOWED_USERS=channel:3` |

Display names are not used for auth — they're mutable and ambiguous.
Use `channel:<idx>` in `MESHCORE_ALLOWED_USERS` to trust an entire channel.

## Requirements

- Hermes Agent (latest)
- MeshCore companion radio node accessible via TCP
- Python 3.11+ (stdlib only — no external dependencies)
- `git` for plugin installation

## License

MIT
