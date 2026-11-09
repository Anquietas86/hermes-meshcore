# Hermes MeshCore Platform

Connects [Hermes Agent](https://github.com/NousResearch/hermes-agent) to a
[MeshCore](https://meshcore.net/) companion radio over TCP, so the agent can
chat on a LoRa mesh: in channels, and in direct messages.

It speaks the companion binary protocol directly. No dependencies beyond the
Python standard library.

## What it does

- **Channels and DMs.** It answers in the channels you choose, with optional
  @mention gating per channel. It answers DMs too, and the sender's name
  comes from your contact list.
- **Radio-sized replies.** Replies are plain text, split by bytes (not
  characters) with `(1/3)` markers. A per-reply packet cap keeps a chatty
  model from hogging airtime.
- **Two trust levels.** Admin nodes and admin channels get everything: the
  profile's full toolsets, memory and your profile. Everyone else can still
  ask for general information (weather, the mesh, propagation, anything on
  the web) through read-only tools, `meshcore` and `web` by default. They
  can't see your personal information or change anything. Only admins can
  run gateway slash commands from the mesh.
- **Remote repeater queries** (`meshcore_admin` tool, admins only). These are
  read-only queries to repeaters and room servers:

  | Command | Kind | What it returns |
  |---|---|---|
  | `status` | login | battery, uptime, queue, noise floor, RSSI/SNR, packet and airtime counters |
  | `telemetry` | login | sensor readings (Cayenne LPP) |
  | `neighbours` | login | repeaters heard directly, with SNR |
  | `acl` | login | access list (usually needs an admin password) |
  | `owner` | anonymous | node name and owner info |
  | `clock` | anonymous | remote clock |
  | `regions` | anonymous | allowed regions |
  | `all` | both | status, neighbours, owner and clock, with a single login |

  "Login" queries log in first; a guest login with an empty password works
  on most repeaters. Anonymous queries need a direct route to the repeater.
  Text CLI commands (`ver`, `set …`) only work on a repeater's serial
  console, so they are not offered.
- **Contact lookup** (`meshcore_contact` tool). It looks up a node's pubkey,
  type, hops and location from the local contact cache, with no radio
  traffic.
- **Nearby nodes** (`meshcore_nearby` tool). It lists the repeaters (or any
  nodes) closest to your node, with distance, compass direction and hops,
  worked out from advert positions. No radio traffic.
- **Repeater watch.** Name the repeaters you care about in
  `MESHCORE_WATCH_NODES`. Each hour the bot checks whether each one is still
  advertising, and asks for its battery level with a guest login. It DMs you
  once when one goes quiet or runs low, and again when it recovers. No model
  calls are involved, so it costs nothing.
- **Reliable multi-part replies.** Parts go out one message at a time, and
  each part waits long enough for repeaters to finish relaying the one
  before it. DM parts wait for the recipient's ACK. Without one, the part is
  retried once over a flood route, the way the MeshCore app does it.
- **Final answers only.** The platform registers as a "minimal" display
  surface, so Hermes sends no tool-progress messages, streaming, "working on
  it" updates, reasoning, long-running notices or busy acknowledgements over
  the air. You can still opt into any of them per platform in
  `display.platforms.meshcore`, if you really want to spend airtime on them.
- **Rate limiting.** Each non-admin sender gets 6 messages per 10 minutes by
  default. DM senders get one short "too many messages" reply; on channels
  the bot just stays quiet.
- **Channel context.** When someone @mentions the bot, it also sees the last
  few channel messages, so "what do you think?" makes sense.
- **Cron and `hermes send` delivery** to `MESHCORE_HOME_CHANNEL`, through the
  running gateway's connection.
- **Dashboard tab.** It shows node status and stats, a searchable contact
  list, a remote repeater query form, and settings with a restart button.
  The settings include the node's host and port, with a "Test connection"
  button, and the status card says why the gateway can't connect when it
  can't. If the bot runs in its own Hermes profile, the dashboard finds it
  even when the dashboard itself is on the default profile.
  You can also add and remove channels on the node from it. A `#name`
  channel gets its key from the name, like the MeshCore app. A private
  channel takes a 32-hex-char key, or the node gets a new random one, which
  the dashboard shows once so you can share it.

## Install

```bash
hermes plugins install Anquietas86/hermes-meshcore
hermes plugins enable meshcore-platform
```

Enabling the plugin is also what mounts the dashboard tab's backend. Hermes
only imports a user plugin's `dashboard/api.py` when the plugin is listed in
`plugins.enabled`.

## Configure

Put these in `~/.hermes/.env` (or the profile's `.env`):

```bash
# Required
MESHCORE_HOST=192.168.1.50
MESHCORE_PORT=5000

# Who may talk to the bot. Set this: with no allowlist Hermes denies
# everyone and answers unknown DMs with a pairing code, over the air.
MESHCORE_ALLOWED_USERS=a1b2c3d4e5f6            # pubkeys or 12+ hex-char prefixes
MESHCORE_GROUP_ALLOWED_CHATS=channel:1         # channels where anyone may talk

# Your own node(s): full toolsets, remote repeater queries, slash commands
MESHCORE_ADMIN_NODES=a1b2c3d4e5f6...

# Channels the bot answers in, and which of them need an @mention
MESHCORE_MONITOR_CHANNELS=1
MESHCORE_REQUIRE_MENTION=true                  # true = all, "0,2" = those, empty = none

# Where cron jobs and notifications go
MESHCORE_HOME_CHANNEL=dm:a1b2c3d4e5f6          # or channel:1
```

Then restart: `hermes gateway restart`.

### All settings

| Variable | Default | Meaning |
|---|---|---|
| `MESHCORE_HOST` | (required) | Companion node hostname or IP |
| `MESHCORE_PORT` | `5000` | TCP port |
| `MESHCORE_ALLOWED_USERS` | | Pubkeys or prefixes allowed to DM. Prefixes of 8–11 chars are matched against known contacts |
| `MESHCORE_GROUP_ALLOWED_CHATS` | | `channel:N` list whose members may all talk to the bot |
| `MESHCORE_ALLOW_ALL_USERS` | `false` | Anyone on the mesh may talk to the bot |
| `MESHCORE_MONITOR_CHANNELS` | | Channel indexes to answer in. Empty means DMs only |
| `MESHCORE_REQUIRE_MENTION` | | `true` = every monitored channel, `0,2` = only those, empty = none |
| `MESHCORE_ENABLE_DMS` | `true` | Answer direct messages |
| `MESHCORE_ADMIN_NODES` | | Operator pubkeys (8+ hex chars) |
| `MESHCORE_ADMIN_CHANNELS` | | Channels treated as trusted. Anyone with the channel key can post there |
| `MESHCORE_PUBLIC_TOOLSETS` | `meshcore,web` | Toolsets for non-admin senders. Keep them read-only (`meshcore_admin` is always removed) |
| `MESHCORE_PUBLIC_TOOLS` | | Extra single tools for non-admin senders (for example one read-only MCP tool). Easiest to pick on the dashboard |
| `MESHCORE_ADMIN_TOOLSETS` | | Pin admin toolsets. Default: the profile's own `platform_toolsets` |
| `MESHCORE_HOME_CHANNEL` | | `channel:N` or `dm:<prefix>` for cron delivery |
| `MESHCORE_BOT_NAME` | node name | Name matched for @mentions |
| `MESHCORE_MAX_MESSAGE_BYTES` | `130` | Bytes per packet (40–200) |
| `MESHCORE_MAX_CHUNKS` | `6` | Packets per reply before truncating |
| `MESHCORE_ADVERT_ON_START` | `true` | Flood advert when the gateway starts |
| `MESHCORE_RATE_LIMIT` | `6/10m` | Messages per non-admin sender per window (`s`, `m`, `h`). `off` disables |
| `MESHCORE_CHANNEL_CONTEXT` | `6` | Recent channel messages passed along with a mention (`0` = off) |
| `MESHCORE_CHUNK_DELAY` | auto | Seconds between reply parts. Auto: 3 × the packet's LoRa airtime (at least 2s), or 0.5s after an acknowledged DM part |
| `MESHCORE_WATCH_NODES` | | Repeaters to watch (names or pubkey prefixes) |
| `MESHCORE_WATCH_NOTIFY` | home channel, else your admin node | Where watch alerts go: `channel:N` or `dm:<prefix>` |
| `MESHCORE_WATCH_INTERVAL_MINUTES` | `60` | Minutes between checks |
| `MESHCORE_WATCH_SILENT_HOURS` | `12` | Alert when no advert has been heard for this long |
| `MESHCORE_WATCH_BATTERY_MV` | `3500` | Alert below this battery level (`0` = off) |
| `MESHCORE_WATCH_STATUS` | `true` | Ask each watched repeater for its status (guest login) on every check |
| `MESHCORE_DEBUG` | `false` | Log every frame |

Mentions can be `@Bot hi`, `Bot: hi`, or the MeshCore app's `@[Bot] hi`
anywhere in the message.

### Identity and trust

| | DM | Channel |
|---|---|---|
| Chat id | `dm:<12-hex prefix>` | `channel:<idx>` |
| Sender id | pubkey prefix (cryptographic) | `channel:<idx>:<display name>` (claimed, not verified) |
| Allowlist | `MESHCORE_ALLOWED_USERS` | `MESHCORE_GROUP_ALLOWED_CHATS` |
| Admin | `MESHCORE_ADMIN_NODES` | `MESHCORE_ADMIN_CHANNELS` |

Display names on channels are just text in the packet, so they are never used
for authorization.

## Public channels and your personal info

Hermes puts your memory and `USER.md` profile into every system prompt,
whoever is talking. Left alone, the model assumes everyone on the mesh is
you: it greets strangers by your name and repeats your details. So each
message now carries a note that says who sent it (with their sender id), and
whether that sender is you. Only your verified radio key counts as you. A
name, a claim, or a post on an admin channel does not. For anyone else, the
note says:

- your memory and profile describe a different person, so don't use your
  name, assume shared history, or reveal anything from them;
- on channels, several different people are talking, and every reply is
  broadcast to everyone in radio range.

The note also tells the model to help strangers with general information
(weather, the mesh, propagation, news) and not to change anything on your
behalf for them. Public senders get read-only tools only (`meshcore` and
`web` by default), so no `memory`, files, terminal, cron or MCP servers.

If your weather, propagation or mesh data comes from an MCP server or
another plugin, open just the tools you want on the dashboard's **Public
access** card. It lists every tool the running gateway has, grouped by
toolset, and marks each one:

- **read-only**: the MCP server says so (its `readOnlyHint`), or it's a
  built-in lookup;
- **can change things**: the MCP server says it isn't read-only;
- **not marked**: nobody said. Treat it as able to change things.

Ticked tools are saved to `MESHCORE_PUBLIC_TOOLS` and apply after a
gateway restart. Whole toolsets still go in `MESHCORE_PUBLIC_TOOLSETS`.

This is enforced in two places. The tool list handed to Hermes for a
public sender leaves out MCP servers except the ones behind tools you picked, and a
`pre_tool_call` hook blocks any tool outside the public list in a
non-admin MeshCore session, whatever Hermes enabled. If that check
itself fails, it blocks.

Admin channels are treated as your people: the model may use memory and
your profile there, but it doesn't assume the sender is you, because
anyone with the channel key can post.

Prompting lowers the risk, but it is not a hard guarantee. If the bot faces
the public, the reliable fix is a separate Hermes profile for the mesh bot.
Turn off memory injection in that profile's `config.yaml`, so your personal
details are never in its prompt at all:

```yaml
memory:
  memory_enabled: false
  user_profile_enabled: false
```

## How it behaves on a flaky link

- One TCP connection per node. A node in use by another profile is refused
  with a clear error, because the companion firmware only serves one client.
- If the link drops, or the node misses two keepalives, the adapter reports a
  retryable error. Hermes's reconnect watcher then rebuilds it with backoff.
  The adapter never fights the gateway by reconnecting on its own.
- Multi-part replies are paced by airtime. At SF11 / 250 kHz, one full
  packet is on air for about 1.4 seconds, and repeaters then relay it. Send
  the next part sooner and it collides with the relays, so parts go missing.
  The old 1-second gap was too short for exactly that reason.
- A part the node itself didn't confirm is not resent. It may already be on air,
  and duplicates garble reception for everyone.
- Messages that arrive while a repeater query is running are still delivered.
  Every frame goes through one reader.

## USB serial (experimental)

Set `MESHCORE_HOST=serial:/dev/ttyUSB0` to talk to a USB-connected
companion at 115200 baud. Standard library only, on Linux and macOS. The
framing is identical to TCP. The automated tests run it over a virtual serial
port, but it has not been tried on real hardware yet. Reports are welcome.

## Multi-profile installs

All state is keyed by the active profile's Hermes home. That includes the
dashboard snapshot, the IPC queue for CLI tools and cron, and the live
adapter lookup. Two profiles can therefore run different nodes side by side,
and one profile never answers with another's radio.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The tests use small stand-ins for the Hermes gateway API (`tests/stubs`) and
a fake companion node that speaks the real frame protocol over TCP
(`tests/fakenode.py`). CI also runs a smoke test against the real
`hermes-agent` checkout.

## Requirements

- Hermes Agent with platform plugin support (`provides_tools` needs a recent
  release)
- A MeshCore companion radio reachable over TCP (WiFi companion firmware,
  or a serial-to-TCP bridge)
- Python 3.11+

## License

MIT
