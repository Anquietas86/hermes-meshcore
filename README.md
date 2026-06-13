# Hermes MeshCore Platform Adapter

Connects [Hermes Agent](https://github.com/NousResearch/hermes-agent) to a
[MeshCore](https://meshcore.net/) companion radio node via TCP, enabling
LoRa mesh communication as a native messaging platform.

## Features

- **Channel monitoring** — respond in mesh channels with @mention detection
- **Direct messages** — private conversations with ACK-guaranteed delivery
- **Node-ID authorization** — admin nodes get full access, public users are restricted
- **150-char enforcement** — respects official MeshCore app message limits
- **Security-aware** — warns the model not to leak credentials in public broadcasts
- **Admin channels** — mark trusted channels for sensitive replies

## Quick Install

```bash
hermes plugins install anquietas/hermes-meshcore
```

Or from the dashboard: Plugins → Install → enter `anquietas/hermes-meshcore`.

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
- `meshcore_py` Python library (auto-installed)
- `git` for plugin installation

## License

MIT
