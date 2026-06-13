# Hermes MeshCore Platform Adapter

Connects [Hermes Agent](https://github.com/NousResearch/hermes-agent) to a
[MeshCore](https://meshcore.net/) companion radio node via TCP, enabling
LoRa mesh communication as a native messaging platform.

Uses the **raw binary protocol** directly — zero external dependencies beyond
Python's standard library. No `meshcore_py` required.

## Features

- **Channel monitoring** — respond in mesh channels with @mention detection
- **Direct messages** — private conversations with fire-and-forget delivery
- **Node-ID authorization** — admin nodes get full access, public users are restricted
- **Message splitting** — auto-splits long responses at word boundaries with (N/M) markers
- **Security-aware** — warns the model not to leak credentials in public broadcasts
- **Admin channels** — mark trusted channels for sensitive replies
- **Radio metadata** — RSSI, SNR, hops, and path info injected into channel context
- **Self-healing** — silence watchdog with automatic reconnect, stale frame drain

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
MESHCORE_ADMIN_NODES=bba647077b2c     # Your node's pubkey prefix
MESHCORE_HOME_CHANNEL=dm:bba647077b2c # Where cron/notifications go
MESHCORE_MONITOR_CHANNELS=1           # Channels to respond in (empty = discover only)
MESHCORE_ENABLE_DMS=true

# Optional
MESHCORE_BOT_NAME=Jarvis
MESHCORE_ADMIN_CHANNELS=1             # Channels trusted for sensitive replies
MESHCORE_REQUIRE_MENTION=true         # Only respond to @Jarvis in channels
```

Then enable in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - platforms/meshcore
```

Restart the gateway:

```bash
hermes gateway restart
```

## Important

Set `approvals.mode: off` in config.yaml — MeshCore's 150-char limit
can't carry approval prompts.

```bash
hermes config set approvals.mode off
```

## Requirements

- Hermes Agent (latest)
- MeshCore companion radio node accessible via TCP
- Python 3.11+ (stdlib only — no external dependencies)
- `git` for plugin installation

## License

MIT
