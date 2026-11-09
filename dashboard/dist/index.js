/**
 * MeshCore dashboard tab: node status, channels, contacts, remote repeater queries and settings.
 * Talks to /api/plugins/meshcore-platform/* through SDK.fetchJSON (which adds dashboard auth).
 * Plain React.createElement, no build step.
 */
(function () {
  "use strict";
  var PLUGIN_NAME = "meshcore-platform";
  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  var React = SDK.React;
  var h = React.createElement;
  var useState = React.useState, useEffect = React.useEffect, useRef = React.useRef;
  var C = SDK.components;

  function api(path, options) {
    return SDK.fetchJSON("/api/plugins/" + PLUGIN_NAME + path, options);
  }
  function post(path, body) {
    return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  }
  function errText(e) {
    if (!e) return "";
    if (e.detail) return String(e.detail);
    if (e.message) return e.message;
    return String(e);
  }
  function dash(v) { return v === null || v === undefined || v === "" ? "—" : String(v); }
  function ago(ts) {
    if (!ts) return "—";
    var s = Date.now() / 1000 - ts;
    if (s < 60) return Math.round(s) + "s ago";
    if (s < 3600) return Math.round(s / 60) + "m ago";
    if (s < 86400) return Math.round(s / 3600) + "h ago";
    return Math.round(s / 86400) + "d ago";
  }
  function uptime(s) {
    if (s == null) return "—";
    var d = Math.floor(s / 86400), hr = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return (d ? d + "d " : "") + hr + "h " + m + "m";
  }

  var CSS = [
    ".mc-grid{display:grid;gap:1rem;grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}",
    ".mc-h{margin:0 0 .75rem;font-size:.95rem;font-weight:600;letter-spacing:.04em;text-transform:uppercase}",
    ".mc-row{display:flex;justify-content:space-between;gap:1rem;padding:.2rem 0;font-size:.85rem;border-bottom:1px solid var(--color-border,rgba(127,127,127,.15))}",
    ".mc-row span:first-child{color:var(--color-muted-foreground,#888)}",
    ".mc-mono{font-family:var(--font-mono,monospace)}",
    ".mc-badge{display:inline-block;padding:.1rem .5rem;border-radius:999px;font-size:.75rem;font-weight:600}",
    ".mc-input{width:100%;padding:.35rem .5rem;border-radius:.375rem;border:1px solid var(--color-border,rgba(127,127,127,.3));background:transparent;color:inherit;font:inherit;font-size:.85rem}",
    ".mc-btn{padding:.35rem .75rem;border-radius:.375rem;border:1px solid var(--color-border,rgba(127,127,127,.3));background:var(--color-secondary,transparent);color:inherit;cursor:pointer;font-size:.85rem}",
    ".mc-btn:disabled{opacity:.5;cursor:default}",
    ".mc-pre{white-space:pre-wrap;word-break:break-word;font-family:var(--font-mono,monospace);font-size:.75rem;max-height:320px;overflow:auto;padding:.5rem;border-radius:.375rem;border:1px solid var(--color-border,rgba(127,127,127,.2))}",
    ".mc-table{width:100%;border-collapse:collapse;font-size:.8rem}.mc-table td,.mc-table th{text-align:left;padding:.25rem .4rem;border-bottom:1px solid var(--color-border,rgba(127,127,127,.15))}",
    ".mc-field{margin-bottom:.6rem}.mc-field label{display:block;font-size:.78rem;margin-bottom:.2rem;color:var(--color-muted-foreground,#888)}",
    ".mc-actions{display:flex;gap:.5rem;flex-wrap:wrap;margin-top:.5rem}",
    ".mc-msg{font-size:.8rem;margin-top:.5rem}"
  ].join("\n");

  function Row(props) {
    return h("div", { className: "mc-row" }, h("span", null, props.label),
      h("span", { className: props.mono ? "mc-mono" : "" }, dash(props.value)));
  }
  function Card(props) {
    return h(C.Card, null, h(C.CardContent, null, h("h3", { className: "mc-h" }, props.title), props.children));
  }

  // ── Status ────────────────────────────────────────────────────────────
  function StatusCard(props) {
    var d = props.data, node = d.node || {}, st = d.stats || {}, ct = d.contacts || {}, radio = node.radio || {};
    var ok = d.connected;
    var color = ok ? "var(--color-success,#16a34a)" : "var(--color-destructive,#dc2626)";
    var label = ok ? "Connected" : (d.fresh === false && d.updated_at ? "Stale (" + Math.round(d.age_s || 0) + "s)" : "Disconnected");
    var _a = useState(null), msg = _a[0], setMsg = _a[1];
    var channels = d.channels || {};
    function advert() {
      setMsg("Sending advert…");
      post("/advert").then(function () { setMsg("Advert queued"); }).catch(function (e) { setMsg(errText(e)); });
    }
    return h(Card, { title: "📡 Node" },
      h("span", { className: "mc-badge", style: { background: color + "22", color: color } }, label),
      d.error && h("div", { className: "mc-msg" }, d.error),
      !ok && d.last_error && h("div", { className: "mc-msg", style: { color: "var(--color-destructive,#dc2626)" } },
        "Last error (" + ago(d.last_error.at) + "): " + d.last_error.message),
      h(Row, { label: "Gateway", value: d.host ? d.host + ":" + d.port : null, mono: true }),
      h(Row, { label: "Hermes profile", value: d.profile }),
      h(Row, { label: "Name", value: node.name }),
      h(Row, { label: "Pubkey", value: node.pubkey_prefix, mono: true }),
      h(Row, { label: "Firmware", value: [node.model, node.firmware].filter(Boolean).join(" ") }),
      h(Row, { label: "Radio", value: radio.freq_mhz ? radio.freq_mhz + " MHz · BW " + radio.bw_khz + " · SF" + radio.sf + " · CR" + radio.cr : null }),
      h(Row, { label: "Battery", value: st.battery_mv ? (st.battery_mv / 1000).toFixed(2) + " V" : null }),
      h(Row, { label: "Uptime", value: uptime(st.uptime_s) }),
      h(Row, { label: "Noise / RSSI / SNR", value: [st.noise, st.rssi, st.snr].map(dash).join(" / ") }),
      h(Row, { label: "Packets rx / tx", value: dash(st.rx_packets) + " / " + dash(st.tx_packets) }),
      h(Row, { label: "Contacts", value: dash(ct.total) + " (" + dash(ct.repeaters) + " repeaters, " + dash(ct.clients) + " clients, " + dash(ct.rooms) + " rooms)" }),
      h(Row, { label: "Channels", value: Object.keys(channels).map(function (k) { return k + ": " + channels[k]; }).join(", ") }),
      h(Row, { label: "Last message", value: ago(d.last_message_at) }),
      h("div", { className: "mc-actions" }, h("button", { className: "mc-btn", onClick: advert, disabled: !ok }, "📣 Send flood advert")),
      msg && h("div", { className: "mc-msg" }, msg)
    );
  }

  // ── Contacts ──────────────────────────────────────────────────────────
  function ContactsCard(props) {
    var _a = useState([]), contacts = _a[0], setContacts = _a[1];
    var _b = useState(""), q = _b[0], setQ = _b[1];
    var _c = useState(null), err = _c[0], setErr = _c[1];
    useEffect(function () {
      api("/contacts").then(function (d) { setContacts(d.contacts || []); setErr(null); })
        .catch(function (e) { setErr(errText(e)); });
    }, [props.connected]);  // reload once the gateway (re)connects
    var ql = q.toLowerCase();
    var shown = contacts.filter(function (c) {
      return !ql || (c.name || "").toLowerCase().indexOf(ql) >= 0 || (c.pubkey || "").indexOf(ql) === 0;
    }).slice(0, 200);
    return h(Card, { title: "👥 Contacts" },
      h("input", { className: "mc-input", placeholder: "Filter by name or pubkey…", value: q, onChange: function (e) { setQ(e.target.value); } }),
      err && h("div", { className: "mc-msg" }, err),
      h("div", { style: { maxHeight: "360px", overflow: "auto", marginTop: ".5rem" } },
        h("table", { className: "mc-table" },
          h("thead", null, h("tr", null, ["Name", "Type", "Hops", "Pubkey", "Heard"].map(function (t) { return h("th", { key: t }, t); }))),
          h("tbody", null, shown.map(function (c) {
            return h("tr", { key: c.pubkey },
              h("td", null, c.name || "—"), h("td", null, c.type), h("td", null, c.hops < 0 ? "flood" : dash(c.hops)),
              h("td", { className: "mc-mono" }, c.pubkey_prefix), h("td", null, ago(c.last_advert)));
          }))
        )
      )
    );
  }

  // ── Remote admin ──────────────────────────────────────────────────────
  function AdminCard(props) {
    var _a = useState([]), nodes = _a[0], setNodes = _a[1];
    var _b = useState({}), commands = _b[0], setCommands = _b[1];
    var _c = useState(""), node = _c[0], setNode = _c[1];
    var _d = useState("status"), cmd = _d[0], setCmd = _d[1];
    var _e = useState(false), busy = _e[0], setBusy = _e[1];
    var _f = useState(null), result = _f[0], setResult = _f[1];
    var timer = useRef(null);
    useEffect(function () {
      api("/nodes").then(function (d) { setNodes(d.nodes || []); setCommands(d.commands || {}); }).catch(function () {});
      return function () { if (timer.current) clearInterval(timer.current); };
    }, []);
    function run() {
      if (!node) { setResult({ error: "Pick a node first" }); return; }
      setBusy(true); setResult(null);
      post("/admin/query", { node: node, command: cmd }).then(function (r) {
        var started = Date.now();
        var limit = cmd === "all" ? 260000 : 110000;
        timer.current = setInterval(function () {
          api("/admin/result?request_id=" + encodeURIComponent(r.request_id)).then(function (res) {
            if (res.status === "complete") {
              clearInterval(timer.current); setBusy(false); setResult(res.result);
            } else if (Date.now() - started > limit) {
              clearInterval(timer.current); setBusy(false);
              setResult({ error: res.queued ? "The gateway never picked the request up" : "No answer from the repeater in time" });
            }
          }).catch(function (e) { clearInterval(timer.current); setBusy(false); setResult({ error: errText(e) }); });
        }, 2000);
      }).catch(function (e) { setBusy(false); setResult({ error: errText(e) }); });
    }
    var names = Object.keys(commands);
    if (!names.length) names = ["status", "telemetry", "neighbours", "acl", "owner", "clock", "regions"];
    return h(Card, { title: "🛰️ Remote repeater query" },
      !props.adminEnabled && h("div", { className: "mc-msg" }, "Remote admin is disabled. Set MESHCORE_ADMIN_NODES to enable it."),
      h("div", { className: "mc-field" }, h("label", null, "Node"),
        h("select", { className: "mc-input", value: node, onChange: function (e) { setNode(e.target.value); } },
          h("option", { value: "" }, "— select a repeater or room —"),
          nodes.map(function (n) {
            return h("option", { key: n.pubkey, value: n.pubkey_prefix },
              (n.name || n.pubkey_prefix) + (n.hops >= 0 ? " (" + n.hops + " hops)" : " (flood)"));
          }))),
      h("div", { className: "mc-field" }, h("label", null, "Query"),
        h("select", { className: "mc-input", value: cmd, onChange: function (e) { setCmd(e.target.value); } },
          names.map(function (n) { return h("option", { key: n, value: n }, n + (commands[n] ? " — " + commands[n] : "")); }),
          h("option", { value: "all" }, "all — status, neighbours, owner, clock"))),
      h("div", { className: "mc-actions" }, h("button", { className: "mc-btn", onClick: run, disabled: busy || !props.adminEnabled }, busy ? "Waiting for the mesh…" : "Run query")),
      result && (result.error && !result.results
        ? h("div", { className: "mc-msg", style: { color: "var(--color-destructive,#dc2626)" } }, result.error)
        : h("pre", { className: "mc-pre" }, JSON.stringify({ results: result.results, errors: result.errors }, null, 2)))
    );
  }

  // ── Channels on the node ───────────────────────────────────────────────
  function waitFor(requestId, onDone) {
    var tries = 0;
    var t = setInterval(function () {
      tries += 1;
      api("/admin/result?request_id=" + encodeURIComponent(requestId)).then(function (res) {
        if (res.status === "complete") { clearInterval(t); onDone(res.result || {}); }
        else if (tries > 30) { clearInterval(t); onDone({ error: "No answer from the gateway yet" }); }
      }).catch(function (e) { clearInterval(t); onDone({ error: errText(e) }); });
    }, 1000);
  }

  function ChannelsCard(props) {
    var d = props.data, channels = d.channels || {}, max = d.max_channels || 8;
    var monitored = ((d.config || {}).monitor_channels || []).map(String);
    var used = Object.keys(channels);
    var free = [];
    for (var i = 0; i < max; i++) if (used.indexOf(String(i)) < 0) free.push(String(i));
    var _a = useState(""), idx = _a[0], setIdx = _a[1];
    var _b = useState(""), name = _b[0], setName = _b[1];
    var _c = useState(""), key = _c[0], setKey = _c[1];
    var _d = useState(null), msg = _d[0], setMsg = _d[1];
    var _e = useState(false), busy = _e[0], setBusy = _e[1];
    var slot = idx || free[0] || "";
    var hashtag = name.trim().charAt(0) === "#";
    function finish(r) {
      setBusy(false);
      if (r.success === false || r.error) { setMsg("Failed: " + (r.error || "unknown error")); return; }
      var text = r.name ? "Channel " + r.index + " set to " + r.name + "." : "Channel " + r.index + " removed.";
      if (r.channel_key) text += " Private key (share it with members): " + r.channel_key;
      if (r.hint) text += " " + r.hint;
      setMsg(text);
    }
    function add() {
      setBusy(true); setMsg("Updating the node…");
      post("/channels", { index: Number(slot), name: name.trim(), channel_key: hashtag ? "" : key.trim() })
        .then(function (r) { setName(""); setKey(""); setIdx(""); waitFor(r.request_id, finish); })
        .catch(function (e) { setBusy(false); setMsg(errText(e)); });
    }
    function remove(i) {
      if (!window.confirm("Remove channel " + i + " (" + channels[i] + ") from the node?")) return;
      setBusy(true); setMsg("Updating the node…");
      post("/channels/remove", { index: Number(i) })
        .then(function (r) { waitFor(r.request_id, finish); })
        .catch(function (e) { setBusy(false); setMsg(errText(e)); });
    }
    return h(Card, { title: "📻 Channels" },
      h("table", { className: "mc-table" },
        h("thead", null, h("tr", null, ["#", "Name", "Bot answers", ""].map(function (t) { return h("th", { key: t }, t); }))),
        h("tbody", null, used.map(function (i) {
          return h("tr", { key: i },
            h("td", null, i), h("td", null, channels[i]),
            h("td", null, monitored.indexOf(i) >= 0 ? "yes" : "no"),
            h("td", null, h("button", { className: "mc-btn", disabled: busy || !d.connected, onClick: function () { remove(i); } }, "Remove")));
        }))),
      h("div", { className: "mc-field", style: { marginTop: ".75rem" } },
        h("label", null, "Add a channel"),
        h("div", { className: "mc-actions" },
          h("select", { className: "mc-input", style: { width: "5rem" }, value: slot, onChange: function (e) { setIdx(e.target.value); } },
            (free.length ? free : used).map(function (i) { return h("option", { key: i, value: i }, i + (channels[i] ? " (replace)" : "")); })),
          h("input", { className: "mc-input", style: { flex: 1 }, placeholder: "#hashtag or private channel name", value: name,
            onChange: function (e) { setName(e.target.value); } }))),
      !hashtag && name.trim() && h("div", { className: "mc-field" },
        h("label", null, "Private channel key (32 hex chars). Leave empty to create a new channel"),
        h("input", { className: "mc-input mc-mono", value: key, placeholder: "0123456789abcdef0123456789abcdef",
          onChange: function (e) { setKey(e.target.value); } })),
      h("div", { className: "mc-actions" },
        h("button", { className: "mc-btn", onClick: add, disabled: busy || !d.connected || !name.trim() || slot === "" }, "➕ Save to node")),
      h("div", { className: "mc-msg" }, "Hashtag channels get their key from the exact name, so match the spelling and case others use. " +
        "Use Settings to choose which channels the bot answers in."),
      msg && h("div", { className: "mc-msg" }, msg)
    );
  }

  // ── Settings ──────────────────────────────────────────────────────────
  var NODE_FIELDS = [
    { key: "host", label: "Node IP or hostname", hint: "192.168.1.50 (or serial:/dev/ttyUSB0)" },
    { key: "port", label: "TCP port", hint: "5000" }
  ];
  var TARGET_FIELDS = [
    { key: "home_channel", label: "Home channel (startup notice, cron jobs)", hint: "dm:3d22d2f1b71e or channel:1" },
    { key: "watch_notify", label: "Repeater watch alerts go to", hint: "empty = home channel" }
  ];
  var FIELDS = [
    { key: "monitor_channels", label: "Channels to answer in (indexes)", hint: "1,2" },
    { key: "require_mention", label: "Channels that need an @mention", hint: "true = all, or 0,2" },
    { key: "group_allowed_chats", label: "Channels anyone may use", hint: "channel:1,channel:2" },
    { key: "admin_channels", label: "Trusted admin channels", hint: "e.g. 3" },
    { key: "admin_nodes", label: "Admin node pubkeys (12+ hex chars)", hint: "a1b2c3d4e5f6" },
    { key: "allowed_users", label: "Allowed DM senders (12+ hex chars)", hint: "a1b2c3d4e5f6,…" },
    { key: "public_toolsets", label: "Whole toolsets for everyone else", hint: "meshcore,web (default). Pick single tools under Public access" }
  ];
  var TOGGLES = [{ key: "enable_dms", label: "Answer DMs" }, { key: "allow_all_users", label: "Allow everyone (dev only)" }];

  function SettingsCard(props) {
    var _a = useState(null), cfg = _a[0], setCfg = _a[1];
    var _b = useState({}), edits = _b[0], setEdits = _b[1];
    var _c = useState(null), msg = _c[0], setMsg = _c[1];
    var _d = useState(false), busy = _d[0], setBusy = _d[1];
    var _e = useState([]), people = _e[0], setPeople = _e[1];
    function load() { api("/config").then(function (d) { setCfg(d); setEdits({}); }).catch(function (e) { setMsg(errText(e)); }); }
    useEffect(load, []);
    useEffect(function () {
      api("/contacts").then(function (d) {
        setPeople((d.contacts || []).filter(function (c) { return c.type === "client"; }));
      }).catch(function () {});
    }, [props.connected]);
    if (!cfg) return h(Card, { title: "⚙️ Settings" }, msg || "Loading…");
    function val(k) { return Object.prototype.hasOwnProperty.call(edits, k) ? edits[k] : (cfg[k] || ""); }
    function set(k, v) { var n = Object.assign({}, edits); n[k] = v; setEdits(n); }
    function save() {
      setBusy(true);
      post("/config", edits).then(function (r) { setCfg(r.config); setEdits({}); setMsg("Saved. Restart the gateway to apply."); })
        .catch(function (e) { setMsg("Not saved: " + errText(e)); }).then(function () { setBusy(false); });
    }
    function test() {
      setBusy(true); setMsg("Testing connection…");
      post("/test-connection", { host: val("host"), port: val("port") }).then(function (r) {
        if (r.skipped) setMsg(r.message + ", so there's nothing to test (and a test would knock it off).");
        else if (r.success) setMsg("✅ Reached " + (r.node_name || "the node") + " (" + r.pubkey_prefix + ") at " + r.host + ":" + r.port + " in " + r.ms + " ms.");
        else setMsg("❌ " + r.host + ":" + r.port + ": " + r.error);
      }).catch(function (e) { setMsg(errText(e)); }).then(function () { setBusy(false); });
    }
    function strangers(k) {
      // Keys in a pubkey list that match none of the node's client contacts.
      if (!people.length || (k !== "admin_nodes" && k !== "allowed_users")) return [];
      return String(val(k) || "").toLowerCase().split(",").map(function (s) { return s.trim(); }).filter(function (key) {
        return key && !people.some(function (c) { return c.pubkey.indexOf(key) === 0; });
      });
    }
    function field(f) {
      var odd = strangers(f.key);
      return h("div", { key: f.key, className: "mc-field" }, h("label", null, f.label),
        h("input", { className: "mc-input", value: val(f.key), placeholder: f.hint, onChange: function (e) { set(f.key, e.target.value); } }),
        odd.length > 0 && h("div", { className: "mc-msg", style: { color: "var(--color-destructive,#dc2626)" } },
          "⚠️ Not in the node's contacts: " + odd.join(", ")));
    }
    function targetField(f) {
      var v = String(val(f.key) || "").trim().toLowerCase();
      var unknown = v.indexOf("dm:") === 0 && people.length > 0 && !people.some(function (c) { return c.pubkey.indexOf(v.slice(3)) === 0; });
      return h("div", { key: f.key, className: "mc-field" }, h("label", null, f.label),
        h("div", { style: { display: "flex", gap: ".5rem" } },
          h("input", { className: "mc-input", style: { flex: 1 }, value: val(f.key), placeholder: f.hint, onChange: function (e) { set(f.key, e.target.value); } }),
          people.length > 0 && h("select", { className: "mc-input", style: { width: "11rem" }, value: "",
            onChange: function (e) { if (e.target.value) set(f.key, e.target.value); } },
            h("option", { value: "" }, "Pick a contact…"),
            people.map(function (c) { return h("option", { key: c.pubkey, value: "dm:" + c.pubkey_prefix }, c.name + " (" + c.pubkey_prefix.slice(0, 6) + ")"); }))),
        unknown && h("div", { className: "mc-msg", style: { color: "var(--color-destructive,#dc2626)" } },
          "⚠️ That key isn't in the node's contacts, so messages to it go nowhere."));
    }
    function restart() {
      setBusy(true);
      post("/restart").then(function () { setMsg("Restart requested."); })
        .catch(function (e) { setMsg(errText(e)); }).then(function () { setBusy(false); });
    }
    return h(Card, { title: "⚙️ Settings" },
      h("div", { style: { fontWeight: 600, fontSize: ".85rem", margin: ".25rem 0" } }, "Node connection"),
      NODE_FIELDS.map(field),
      h("div", { className: "mc-actions" },
        h("button", { className: "mc-btn", onClick: test, disabled: busy || !val("host") }, "🔌 Test connection")),
      h("div", { style: { fontWeight: 600, fontSize: ".85rem", margin: ".75rem 0 .25rem" } }, "Notifications"),
      TARGET_FIELDS.map(targetField),
      h("div", { style: { fontWeight: 600, fontSize: ".85rem", margin: ".75rem 0 .25rem" } }, "Behaviour"),
      FIELDS.map(field),
      TOGGLES.map(function (t) {
        return h("label", { key: t.key, style: { display: "block", fontSize: ".85rem", margin: ".25rem 0" } },
          h("input", { type: "checkbox", checked: String(val(t.key)).toLowerCase() === "true" || (t.key === "enable_dms" && val(t.key) === ""),
            onChange: function (e) { set(t.key, e.target.checked ? "true" : "false"); } }), " " + t.label);
      }),
      h("div", { className: "mc-actions" },
        h("button", { className: "mc-btn", onClick: save, disabled: busy || !Object.keys(edits).length }, "💾 Save"),
        h("button", { className: "mc-btn", onClick: load, disabled: busy }, "↩ Revert"),
        h("button", { className: "mc-btn", onClick: restart, disabled: busy }, "🔄 Restart gateway")),
      msg && h("div", { className: "mc-msg" }, msg)
    );
  }

  // ── Public access ─────────────────────────────────────────────────────
  function splitList(v) { return String(v || "").split(",").map(function (x) { return x.trim(); }).filter(Boolean); }
  function PublicAccessCard(props) {
    var _a = useState(null), d = _a[0], setD = _a[1];
    var _b = useState(null), chosen = _b[0], setChosen = _b[1];
    var _c = useState(null), msg = _c[0], setMsg = _c[1];
    var _d = useState(""), q = _d[0], setQ = _d[1];
    var _e = useState(false), busy = _e[0], setBusy = _e[1];
    function load() {
      api("/tools").then(function (r) { setD(r); setChosen(splitList(r.public_tools)); })
        .catch(function (e) { setMsg(errText(e)); });
    }
    useEffect(load, [props.connected]);
    if (!d || !chosen) return h(Card, { title: "🔓 Public access" }, msg || "Loading…");
    var toolsets = splitList(d.public_toolsets);
    if (!toolsets.length) toolsets = ["meshcore", "web"];
    var saved = splitList(d.public_tools);
    var dirty = chosen.slice().sort().join(",") !== saved.slice().sort().join(",");
    function covered(t) { return toolsets.indexOf(t.toolset) >= 0 || (d.allowed.indexOf(t.name) >= 0 && saved.indexOf(t.name) < 0); }
    function toggle(name, on) {
      setChosen(on ? chosen.concat([name]) : chosen.filter(function (n) { return n !== name; }));
    }
    function save() {
      setBusy(true);
      post("/config", { public_tools: chosen.join(",") }).then(function () { setMsg("Saved. Restart the gateway to apply."); load(); })
        .catch(function (e) { setMsg("Not saved: " + errText(e)); }).then(function () { setBusy(false); });
    }
    var needle = q.trim().toLowerCase();
    var rows = d.catalog.filter(function (t) {
      return !needle || (t.name + " " + t.toolset + " " + (t.description || "")).toLowerCase().indexOf(needle) >= 0;
    });
    var groups = {};
    rows.forEach(function (t) { (groups[t.toolset || "other"] = groups[t.toolset || "other"] || []).push(t); });
    var risky = chosen.filter(function (n) { var t = d.catalog.find(function (c) { return c.name === n; }); return !t || t.read_only !== true; });
    function badge(t) {
      if (t.read_only === true) return h("span", { className: "mc-badge", style: { background: "#16a34a22", color: "#16a34a" } }, "read-only");
      if (t.read_only === false) return h("span", { className: "mc-badge", style: { background: "#dc262622", color: "#dc2626" } }, "can change things");
      return h("span", { className: "mc-badge", style: { background: "#88888822", color: "#888" } }, "not marked");
    }
    return h(Card, { title: "🔓 Public access" },
      h("div", { className: "mc-msg", style: { marginTop: 0 } },
        "Tools anyone who isn't an admin may use, in DMs and public channels. Admins always get everything. " +
        "Whole toolsets open now: " + toolsets.join(", ") + " (set in Settings)."),
      !d.catalog.length && h("div", { className: "mc-msg" },
        d.connected ? "The gateway hasn't published its tool list yet. Give it a minute after a restart." :
          "The gateway isn't running, so there's no tool list to pick from."),
      d.catalog.length > 0 && h("input", { className: "mc-input", style: { margin: ".5rem 0" }, placeholder: "Filter tools…",
        value: q, onChange: function (e) { setQ(e.target.value); } }),
      h("div", { style: { maxHeight: "360px", overflow: "auto" } },
        Object.keys(groups).sort().map(function (g) {
          return h("div", { key: g, style: { marginBottom: ".5rem" } },
            h("div", { style: { fontWeight: 600, fontSize: ".8rem", margin: ".25rem 0" } }, g),
            groups[g].map(function (t) {
              var isCovered = covered(t);
              return h("label", { key: t.name, title: t.description || "", style: { display: "flex", gap: ".5rem", alignItems: "baseline", fontSize: ".8rem", padding: ".1rem 0" } },
                h("input", { type: "checkbox", disabled: isCovered, checked: isCovered || chosen.indexOf(t.name) >= 0,
                  onChange: function (e) { toggle(t.name, e.target.checked); } }),
                h("span", { className: "mc-mono" }, t.name), badge(t),
                isCovered && h("span", { style: { color: "var(--color-muted-foreground,#888)" } }, "via " + t.toolset));
            }));
        })),
      risky.length > 0 && h("div", { className: "mc-msg", style: { color: "var(--color-destructive,#dc2626)" } },
        "⚠️ Not marked read-only, so strangers could use these to change things: " + risky.join(", ")),
      h("div", { className: "mc-actions" },
        h("button", { className: "mc-btn", onClick: save, disabled: busy || !dirty }, "💾 Save"),
        h("button", { className: "mc-btn", onClick: load, disabled: busy }, "↩ Revert")),
      msg && h("div", { className: "mc-msg" }, msg));
  }

  // ── Page ──────────────────────────────────────────────────────────────
  function MeshCorePage() {
    var _a = useState(null), data = _a[0], setData = _a[1];
    var _b = useState(null), err = _b[0], setErr = _b[1];
    useEffect(function () {
      var live = true;
      function poll() {
        api("/status").then(function (d) { if (live) { setData(d); setErr(null); } })
          .catch(function (e) { if (live) setErr(errText(e)); });
      }
      poll();
      var t = setInterval(poll, 5000);
      return function () { live = false; clearInterval(t); };
    }, []);
    if (!data) return h(Card, { title: "📡 MeshCore" }, err ? "Dashboard backend unavailable: " + err : "Loading…");
    var adminEnabled = !!((data.config || {}).admin_enabled);
    return h("div", null, h("style", null, CSS),
      h("div", { className: "mc-grid" },
        h(StatusCard, { data: data }),
        h(AdminCard, { adminEnabled: adminEnabled }),
        h(ChannelsCard, { data: data }),
        h(ContactsCard, { connected: !!data.connected }),
        h(SettingsCard, { connected: !!data.connected }),
        h(PublicAccessCard, { connected: !!data.connected })));
  }

  window.__HERMES_PLUGINS__.register(PLUGIN_NAME, MeshCorePage);
})();
