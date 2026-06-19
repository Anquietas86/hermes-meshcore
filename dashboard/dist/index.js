/**
 * MeshCore Dashboard Tab — live node status, contacts, and radio stats.
 * Polls /api/plugins/meshcore-platform/status every 5 seconds.
 * Uses the Hermes dashboard plugin SDK for React, components, and auth.
 */
(function () {
  "use strict";
  const PLUGIN_NAME = "meshcore-platform";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const hooks = SDK.hooks;
  const C = SDK.components;
  const cn = SDK.utils.cn;

  // ── API helper (uses SDK.fetchJSON for auth) ──────────────────────────
  function api(path, options) {
    const url = "/api/plugins/meshcore-platform" + path;
    return SDK.fetchJSON(url, options);
  }

  // ── Helpers ───────────────────────────────────────────────────────────
  function escapeHtml(s) {
    if (!s) return "—";
    const div = document.createElement("div");
    div.textContent = String(s);
    return div.innerHTML;
  }

  function batteryPct(mv) {
    if (mv == null) return null;
    if (mv > 4200) return 100;
    if (mv > 4000) return 90;
    if (mv > 3800) return 70;
    if (mv > 3600) return 40;
    if (mv > 3400) return 15;
    return 5;
  }

  function agoStr(seconds) {
    if (seconds == null) return "—";
    if (seconds < 60) return Math.round(seconds) + "s ago";
    if (seconds < 3600) return Math.round(seconds / 60) + "m ago";
    return Math.round(seconds / 3600) + "h ago";
  }

  function formatTime(ts) {
    if (!ts) return "—";
    var d = new Date(ts * 1000);
    var pad = function (n) { return n < 10 ? "0" + n : String(n); };
    return d.getFullYear() + "-" +
      pad(d.getMonth() + 1) + "-" +
      pad(d.getDate()) + " " +
      pad(d.getHours()) + ":" +
      pad(d.getMinutes()) + ":" +
      pad(d.getSeconds());
  }

  function uptimeStr(s) {
    if (s == null) return "—";
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    return h + "h " + m + "m";
  }

  // ── StatRow component ─────────────────────────────────────────────────
  function StatRow({ label, value, mono }) {
    return React.createElement("div", { className: "mc-stat-row" },
      React.createElement("span", { className: "mc-stat-label" }, label),
      React.createElement("span", {
        className: "mc-stat-value",
        style: mono ? { fontFamily: "var(--font-mono, monospace)" } : {},
      }, value)
    );
  }

  // ── Main MeshCorePage component ───────────────────────────────────────
  function MeshCorePage() {
    const [data, setData] = React.useState(null);
    const [error, setError] = React.useState(null);

    React.useEffect(function () {
      let active = true;
      function poll() {
        if (!active) return;
        api("/status")
          .then(function (d) { if (active) { setData(d); setError(null); } })
          .catch(function (e) { if (active) setError(String(e)); });
      }
      poll();
      const interval = setInterval(poll, 5000);
      return function () { active = false; clearInterval(interval); };
    }, []);

    // ── Error state ───────────────────────────────────────────────────
    if (error && !data) {
      return React.createElement(C.Card, { className: "mc-error-card" },
        React.createElement(C.CardContent, null,
          React.createElement("div", { style: { textAlign: "center", color: "var(--color-muted)" } },
            React.createElement("p", null, "⚠️ MeshCore gateway not reachable"),
            React.createElement("small", null, escapeHtml(error))
          )
        )
      );
    }

    // ── Loading state ─────────────────────────────────────────────────
    if (!data) {
      return React.createElement(C.Card, { className: "mc-loading-card" },
        React.createElement(C.CardContent, null,
          React.createElement("div", { style: { textAlign: "center", color: "var(--color-muted)" } },
            React.createElement("p", null, "Connecting to MeshCore gateway…")
          )
        )
      );
    }

    const connColor = data.connected ? "var(--color-success, #22c55e)" : "var(--color-danger, #ef4444)";
    const connLabel = data.connected ? "🟢 Connected" : "🔴 Disconnected";
    const node = data.node || {};
    const radio = node.radio;
    const stats = data.stats || {};
    const contacts = data.contacts || {};

    return React.createElement("div", { className: "mc-dashboard" },
      // ── Inline styles ──────────────────────────────────────────────
      React.createElement("style", null, `
        .mc-dashboard {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
          gap: 1rem;
          padding: 1rem;
        }
        .mc-stat-row {
          display: flex;
          justify-content: space-between;
          align-items: center;
          padding: 0.35rem 0;
          border-bottom: 1px solid var(--color-border, rgba(255,255,255,0.04));
          font-size: 0.85rem;
        }
        .mc-stat-row:last-child { border-bottom: none; }
        .mc-stat-label { color: var(--color-muted); }
        .mc-stat-value { font-weight: 500; }
        .mc-conn-badge {
          display: inline-block;
          padding: 0.2rem 0.6rem;
          border-radius: var(--radius-sm, 0.25rem);
          font-size: 0.8rem;
          font-weight: 600;
          margin-bottom: 0.75rem;
        }
        .mc-contacts-summary {
          display: flex;
          gap: 1rem;
          flex-wrap: wrap;
        }
        .mc-contact-chip {
          background: var(--color-card, rgba(255,255,255,0.04));
          border: 1px solid var(--color-border, rgba(255,255,255,0.08));
          border-radius: var(--radius-sm, 0.25rem);
          padding: 0.4rem 0.75rem;
          font-size: 0.8rem;
        }
        .mc-contact-chip strong { font-family: var(--font-mono, monospace); }
        .mc-error-card, .mc-loading-card { padding: 1.5rem; }
        .mc-channel-list {
          display: flex;
          flex-direction: column;
          gap: 0.35rem;
        }
        .mc-channel-item {
          display: flex;
          justify-content: space-between;
          align-items: center;
          padding: 0.35rem 0;
          border-bottom: 1px solid var(--color-border, rgba(255,255,255,0.04));
          font-size: 0.85rem;
        }
        .mc-channel-item:last-child { border-bottom: none; }
        .mc-channel-name { font-weight: 500; }
        .mc-channel-badge {
          font-family: var(--font-mono, monospace);
          font-size: 0.75rem;
          color: var(--color-muted);
        }
        .mc-tag-list {
          display: flex;
          flex-wrap: wrap;
          gap: 0.35rem;
        }
        .mc-tag {
          display: inline-block;
          padding: 0.2rem 0.5rem;
          border-radius: var(--radius-sm, 0.25rem);
          font-size: 0.75rem;
          font-family: var(--font-mono, monospace);
        }
        .mc-tag-admin {
          background: rgba(34, 197, 94, 0.12);
          color: var(--color-success, #22c55e);
          border: 1px solid rgba(34, 197, 94, 0.25);
        }
        .mc-tag-channel {
          background: rgba(59, 130, 246, 0.12);
          color: var(--color-info, #3b82f6);
          border: 1px solid rgba(59, 130, 246, 0.25);
        }
        .mc-tag-mention {
          background: rgba(245, 158, 11, 0.12);
          color: var(--color-warning, #f59e0b);
          border: 1px solid rgba(245, 158, 11, 0.25);
        }
        .mc-config-fields {
          display: flex;
          flex-direction: column;
          gap: 0.5rem;
        }
        .mc-config-field {
          display: flex;
          flex-direction: column;
          gap: 0.15rem;
        }
        .mc-config-label {
          font-size: 0.75rem;
          color: var(--color-muted);
          text-transform: uppercase;
          letter-spacing: 0.04em;
        }
        .mc-config-input {
          padding: 0.4rem 0.6rem;
          border: 1px solid var(--color-border, rgba(255,255,255,0.08));
          border-radius: var(--radius-sm, 0.25rem);
          background: var(--color-card, rgba(255,255,255,0.04));
          color: var(--color-text);
          font-size: 0.8rem;
          font-family: var(--font-mono, monospace);
        }
        .mc-config-input:focus {
          outline: none;
          border-color: var(--color-accent, #6366f1);
        }
        .mc-config-actions {
          margin-top: 0.75rem;
          display: flex;
          align-items: center;
        }
        .mc-btn {
          padding: 0.4rem 0.8rem;
          border: 1px solid var(--color-border, rgba(255,255,255,0.08));
          border-radius: var(--radius-sm, 0.25rem);
          font-size: 0.8rem;
          cursor: pointer;
          transition: opacity 0.2s;
        }
        .mc-btn:disabled {
          opacity: 0.5;
          cursor: not-allowed;
        }
        .mc-btn-save {
          background: rgba(99, 102, 241, 0.15);
          color: var(--color-accent, #6366f1);
          border-color: rgba(99, 102, 241, 0.3);
        }
        .mc-btn-save:hover:not(:disabled) {
          background: rgba(99, 102, 241, 0.25);
        }
        .mc-btn-load {
          background: rgba(255,255,255,0.04);
          color: var(--color-muted);
          border-color: var(--color-border, rgba(255,255,255,0.08));
        }
        .mc-btn-load:hover {
          background: rgba(255,255,255,0.08);
          color: var(--color-text);
        }
        .mc-btn-restart {
          background: rgba(239, 68, 68, 0.12);
          color: var(--color-danger, #ef4444);
          border-color: rgba(239, 68, 68, 0.25);
        }
        .mc-btn-restart:hover:not(:disabled) {
          background: rgba(239, 68, 68, 0.2);
        }
        .mc-config-msg {
          margin-top: 0.5rem;
          font-size: 0.8rem;
        }
        .mc-ch-matrix {
          display: flex;
          flex-direction: column;
          gap: 0.15rem;
        }
        .mc-ch-row {
          display: grid;
          grid-template-columns: 1fr 60px 60px 60px;
          align-items: center;
          gap: 0.25rem;
          padding: 0.25rem 0;
          border-bottom: 1px solid var(--color-border, rgba(255,255,255,0.04));
          font-size: 0.8rem;
        }
        .mc-ch-row:last-child { border-bottom: none; }
        .mc-ch-header {
          font-size: 0.7rem;
          color: var(--color-muted);
          text-transform: uppercase;
          letter-spacing: 0.04em;
          border-bottom: 1px solid var(--color-border, rgba(255,255,255,0.08));
          padding-bottom: 0.35rem;
        }
        .mc-ch-name {
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }
        .mc-ch-cb {
          justify-self: center;
          width: 14px;
          height: 14px;
          accent-color: var(--color-accent, #6366f1);
          cursor: pointer;
        }
        .mc-ch-header .mc-ch-cb {
          width: auto;
          height: auto;
          accent-color: unset;
        }
        .mc-bool-row {
          display: flex;
          gap: 1.5rem;
          align-items: center;
        }
        .mc-bool-label {
          display: flex;
          align-items: center;
          gap: 0.35rem;
          font-size: 0.8rem;
          cursor: pointer;
        }
        .mc-bool-label input[type="checkbox"] {
          width: 14px;
          height: 14px;
          accent-color: var(--color-accent, #6366f1);
        }
      `),

      // ── Connection & Contacts card (merged) ──────────────────────────
      React.createElement(C.Card, null,
        React.createElement(C.CardContent, null,
          React.createElement("h3", { style: { margin: "0 0 0.75rem 0", fontSize: "0.9rem", textTransform: "uppercase", letterSpacing: "0.06em", color: "var(--color-muted)" } }, "📡 Connection & Contacts"),
          // Connection section
          React.createElement("div", { style: { marginBottom: "0.5rem" } },
            React.createElement("div", { style: { fontSize: "0.75rem", color: "var(--color-muted)", textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: "0.25rem" } }, "Gateway"),
            React.createElement("div", { className: "mc-conn-badge", style: { background: connColor + "20", color: connColor } }, connLabel),
            React.createElement(StatRow, { label: "Host", value: escapeHtml(data.host) + ":" + (data.port || "—"), mono: true }),
            React.createElement(StatRow, { label: "Last Message", value: formatTime(data.last_message_time), mono: true }),
            React.createElement(StatRow, { label: "DMs", value: data.dms_enabled ? "✅ Enabled" : "❌ Disabled" })
          ),
          // Contacts section
          React.createElement("div", { style: { marginBottom: "0.5rem" } },
            React.createElement("div", { style: { fontSize: "0.75rem", color: "var(--color-muted)", textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: "0.25rem" } }, "Contacts (" + (contacts.total || 0) + ")"),
            React.createElement("div", { className: "mc-contacts-summary" },
              React.createElement("div", { className: "mc-contact-chip" }, "🔁 ", React.createElement("strong", null, contacts.repeaters || 0), " repeaters"),
              React.createElement("div", { className: "mc-contact-chip" }, "📱 ", React.createElement("strong", null, contacts.clients || 0), " clients"),
              React.createElement("div", { className: "mc-contact-chip" }, "🏠 ", React.createElement("strong", null, contacts.rooms || 0), " rooms")
            )
          ),
          // Channels section
          data.channels && data.channels.length > 0 && React.createElement("div", null,
            React.createElement("div", { style: { fontSize: "0.75rem", color: "var(--color-muted)", textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: "0.25rem" } }, "Channels (" + data.channels.length + ")"),
            React.createElement("div", { className: "mc-channel-list" },
              data.channels.map(function (ch) {
                var names = data.channel_names || {};
                var name = names[String(ch)] || "";
                return React.createElement("div", { key: ch, className: "mc-channel-item" },
                  React.createElement("span", { className: "mc-channel-name" }, name || ("Channel " + ch)),
                  React.createElement("span", { className: "mc-channel-badge" }, "ch " + ch)
                );
              })
            )
          )
        )
      ),

      // ── Node & Telemetry card ────────────────────────────────────────
      React.createElement(C.Card, null,
        React.createElement(C.CardContent, null,
          React.createElement("h3", { style: { margin: "0 0 0.75rem 0", fontSize: "0.9rem", textTransform: "uppercase", letterSpacing: "0.06em", color: "var(--color-muted)" } }, "🖥️ Node & Telemetry"),
          // Node section
          React.createElement("div", { style: { marginBottom: "0.5rem" } },
            React.createElement("div", { style: { fontSize: "0.75rem", color: "var(--color-muted)", textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: "0.25rem" } }, "Node Info"),
            React.createElement(StatRow, { label: "Name", value: escapeHtml(node.name || "unknown") }),
            React.createElement(StatRow, { label: "Pubkey", value: escapeHtml(node.pubkey_prefix || "—") + "…", mono: true }),
            node.lat != null && React.createElement(StatRow, { label: "Location", value: node.lat.toFixed(4) + ", " + node.lon.toFixed(4), mono: true }),
            radio && React.createElement(React.Fragment, null,
              React.createElement(StatRow, { label: "Frequency", value: radio.freq_mhz != null ? radio.freq_mhz + " MHz" : "—", mono: true }),
              React.createElement(StatRow, { label: "Bandwidth", value: radio.bw_khz != null ? radio.bw_khz + " kHz" : "—", mono: true }),
              React.createElement(StatRow, { label: "SF / CR", value: radio.sf != null ? "SF" + radio.sf + " / 4/" + (radio.cr || "—") : "—", mono: true })
            )
          ),
          // Telemetry section
          React.createElement("div", null,
            React.createElement("div", { style: { fontSize: "0.75rem", color: "var(--color-muted)", textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: "0.25rem" } }, "Telemetry"),
            stats.battery_mv != null && (function () {
              const pct = batteryPct(stats.battery_mv);
              const batColor = pct > 50 ? "var(--color-success, #22c55e)" : pct > 20 ? "var(--color-warning, #f59e0b)" : "var(--color-danger, #ef4444)";
              return React.createElement(StatRow, {
                label: "Battery",
                value: React.createElement("span", { style: { color: batColor } }, (stats.battery_mv / 1000).toFixed(2) + "V (~" + pct + "%)"),
              });
            })(),
            React.createElement(StatRow, { label: "Uptime", value: uptimeStr(stats.uptime_s) }),
            React.createElement(StatRow, { label: "Packets", value: "TX: " + (stats.tx_packets ?? "—") + " / RX: " + (stats.rx_packets ?? "—"), mono: true }),
            React.createElement(StatRow, { label: "Signal", value: "Noise: " + (stats.noise ?? "—") + " | RSSI: " + (stats.rssi ?? "—") + " | SNR: " + (stats.snr != null ? stats.snr + "dB" : "—"), mono: true })
          )
        )
      ),

      // ── Configuration card ──────────────────────────────────────────
      React.createElement(ConfigCard, { channels: data.channels, channelNames: data.channel_names })
    );
  }

  // ── ConfigCard sub-component ────────────────────────────────────────
  function ConfigCard(props) {
    var channels = props.channels || [];
    var channelNames = props.channelNames || {};

    var _React = React;
    var useState = _React.useState;
    var useEffect = _React.useEffect;

    var _a = useState(null), config = _a[0], setConfig = _a[1];
    var _b = useState({}), edits = _b[0], setEdits = _b[1];
    var _c = useState(false), saving = _c[0], setSaving = _c[1];
    var _d = useState(false), restarting = _d[0], setRestarting = _d[1];
    var _e = useState(null), saveMsg = _e[0], setSaveMsg = _e[1];
    var _h = useState(false), loading = _h[0], setLoading = _h[1];

    // Channel checkbox state: { chIndex: { monitor: bool, admin: bool, mention: bool } }
    var _f = useState({}), chChecks = _f[0], setChChecks = _f[1];
    // Boolean toggle state
    var _g = useState({ allow_all_users: false, enable_dms: false }), boolToggles = _g[0], setBoolToggles = _g[1];

    useEffect(function () {
      api("/config")
        .then(function (d) {
          setConfig(d);
          // Parse current config into checkbox state
          var checks = {};
          var adminChs = parseCsv(d.admin_channels || "");
          var monitorChs = parseCsv(d.monitor_channels || "");
          var mentionChs = parseCsv(d.require_mention_channels || "");
          channels.forEach(function (ch) {
            var chStr = String(ch);
            checks[chStr] = {
              monitor: monitorChs.indexOf(chStr) !== -1,
              admin: adminChs.indexOf(chStr) !== -1,
              mention: mentionChs.indexOf(chStr) !== -1,
            };
          });
          setChChecks(checks);
          // Parse boolean toggles
          setBoolToggles({
            allow_all_users: String(d.allow_all_users || "").toLowerCase() === "true",
            enable_dms: String(d.enable_dms || "").toLowerCase() === "true",
          });
        })
        .catch(function () {});
    }, []);

    if (!config) return null;
    if (config.error) return null;

    // Parse comma-separated string to array of trimmed strings
    function parseCsv(s) {
      if (!s) return [];
      return s.split(",").map(function (x) { return x.trim(); }).filter(Boolean);
    }

    function handleChange(key, value) {
      var next = {};
      for (var k in edits) { next[k] = edits[k]; }
      next[key] = value;
      setEdits(next);
    }

    function toggleChannel(ch, col) {
      var next = {};
      for (var k in chChecks) { next[k] = Object.assign({}, chChecks[k]); }
      if (!next[ch]) next[ch] = { monitor: false, admin: false, mention: false };
      next[ch][col] = !next[ch][col];
      setChChecks(next);
    }

    function toggleBool(key) {
      var next = Object.assign({}, boolToggles);
      next[key] = !next[key];
      setBoolToggles(next);
    }

    function handleSave() {
      // Build channel config from checkbox state
      var monitorChs = [];
      var adminChs = [];
      var mentionChs = [];
      for (var ch in chChecks) {
        if (chChecks[ch].monitor) monitorChs.push(ch);
        if (chChecks[ch].admin) adminChs.push(ch);
        if (chChecks[ch].mention) mentionChs.push(ch);
      }
      var channelEdits = {
        monitor_channels: monitorChs.join(", "),
        admin_channels: adminChs.join(", "),
        require_mention_channels: mentionChs.join(", "),
      };

      // Boolean toggle edits
      var boolEdits = {
        allow_all_users: boolToggles.allow_all_users ? "true" : "false",
        enable_dms: boolToggles.enable_dms ? "true" : "false",
      };

      // Merge text field edits with channel checkbox edits + bool toggles
      var changed = {};
      var hasChanges = false;
      for (var k in edits) {
        if (edits[k] !== config[k]) {
          changed[k] = edits[k];
          hasChanges = true;
        }
      }
      for (var ck in channelEdits) {
        if (channelEdits[ck] !== (config[ck] || "")) {
          changed[ck] = channelEdits[ck];
          hasChanges = true;
        }
      }
      for (var bk in boolEdits) {
        if (boolEdits[bk] !== (config[bk] || "")) {
          changed[bk] = boolEdits[bk];
          hasChanges = true;
        }
      }
      if (!hasChanges) { setSaveMsg("No changes to save"); return; }
      setSaving(true);
      setSaveMsg(null);
      api("/config", { method: "POST", body: JSON.stringify(changed) })
        .then(function (d) {
          setConfig(d.config);
          setEdits({});
          setSaving(false);
          setSaveMsg("✅ Saved — restart gateway to apply");
        })
        .catch(function (e) {
          setSaving(false);
          setSaveMsg("❌ Save failed: " + String(e));
        });
    }

    function handleRestart() {
      setRestarting(true);
      setSaveMsg(null);
      api("/restart", { method: "POST" })
        .then(function (d) {
          setRestarting(false);
          setSaveMsg(d.success ? "✅ Gateway restarting…" : "❌ Restart failed: " + (d.error || d.stderr));
        })
        .catch(function (e) {
          setRestarting(false);
          setSaveMsg("❌ Restart failed: " + String(e));
        });
    }

    function handleLoad() {
      console.log("handleLoad called");
      setLoading(true);
      setSaveMsg(null);
      api("/config")
        .then(function (d) {
          console.log("Load config response:", d);
          setConfig(d);
          setEdits({});
          setSaveMsg("✅ Loaded current settings");
          // Reset channel checkboxes
          var checks = {};
          var adminChs = parseCsv(d.admin_channels || "");
          var monitorChs = parseCsv(d.monitor_channels || "");
          var mentionChs = parseCsv(d.require_mention_channels || "");
          channels.forEach(function (ch) {
            var chStr = String(ch);
            checks[chStr] = {
              monitor: monitorChs.indexOf(chStr) !== -1,
              admin: adminChs.indexOf(chStr) !== -1,
              mention: mentionChs.indexOf(chStr) !== -1,
            };
          });
          setChChecks(checks);
          // Reset boolean toggles
          setBoolToggles({
            allow_all_users: String(d.allow_all_users || "").toLowerCase() === "true",
            enable_dms: String(d.enable_dms || "").toLowerCase() === "true",
          });
          setLoading(false);
        })
        .catch(function (e) {
          console.error("Load config error:", e);
          setLoading(false);
          setSaveMsg("❌ Load failed: " + String(e));
        });
    }

    var textFields = [
      { key: "admin_nodes", label: "Admin Nodes", hint: "pubkey prefixes, comma-separated" },
      { key: "allowed_users", label: "Allowed Users", hint: "whitelisted pubkey prefixes" },
    ];

    return React.createElement(C.Card, null,
      React.createElement(C.CardContent, null,
        React.createElement("h3", { style: { margin: "0 0 0.75rem 0", fontSize: "0.9rem", textTransform: "uppercase", letterSpacing: "0.06em", color: "var(--color-muted)" } }, "⚙️ Configuration"),

        // ── Channel checkbox matrix ──────────────────────────────────
        channels.length > 0 && React.createElement("div", { style: { marginBottom: "0.75rem" } },
          React.createElement("div", { className: "mc-config-label", style: { marginBottom: "0.35rem" } }, "Channel Roles"),
          React.createElement("div", { className: "mc-ch-matrix" },
            // Header row
            React.createElement("div", { className: "mc-ch-row mc-ch-header" },
              React.createElement("span", { className: "mc-ch-name" }, "Channel"),
              React.createElement("span", { className: "mc-ch-cb" }, "Monitor"),
              React.createElement("span", { className: "mc-ch-cb" }, "Admin"),
              React.createElement("span", { className: "mc-ch-cb" }, "Mention")
            ),
            channels.map(function (ch) {
              var chStr = String(ch);
              var name = channelNames[chStr] || ("Channel " + ch);
              var ck = chChecks[chStr] || { monitor: false, admin: false, mention: false };
              return React.createElement("div", { key: ch, className: "mc-ch-row" },
                React.createElement("span", { className: "mc-ch-name" }, name + " (ch " + ch + ")"),
                React.createElement("input", {
                  type: "checkbox",
                  className: "mc-ch-cb",
                  checked: ck.monitor,
                  onChange: function () { toggleChannel(chStr, "monitor"); },
                  title: "Monitor this channel",
                }),
                React.createElement("input", {
                  type: "checkbox",
                  className: "mc-ch-cb",
                  checked: ck.admin,
                  onChange: function () { toggleChannel(chStr, "admin"); },
                  title: "Trusted — no @mention required",
                }),
                React.createElement("input", {
                  type: "checkbox",
                  className: "mc-ch-cb",
                  checked: ck.mention,
                  onChange: function () { toggleChannel(chStr, "mention"); },
                  title: "Require @mention to respond",
                })
              );
            })
          )
        ),

        // ── Boolean toggles ───────────────────────────────────────────
        React.createElement("div", { style: { marginBottom: "0.75rem" } },
          React.createElement("div", { className: "mc-config-label", style: { marginBottom: "0.35rem" } }, "Access"),
          React.createElement("div", { className: "mc-bool-row" },
            React.createElement("label", { className: "mc-bool-label" },
              React.createElement("input", {
                type: "checkbox",
                checked: boolToggles.allow_all_users,
                onChange: function () { toggleBool("allow_all_users"); },
              }),
              " Allow All Users"
            ),
            React.createElement("label", { className: "mc-bool-label" },
              React.createElement("input", {
                type: "checkbox",
                checked: boolToggles.enable_dms,
                onChange: function () { toggleBool("enable_dms"); },
              }),
              " Enable DMs"
            )
          )
        ),

        // ── Text fields ──────────────────────────────────────────────
        React.createElement("div", { className: "mc-config-fields" },
          textFields.map(function (f) {
            var val = edits.hasOwnProperty(f.key) ? edits[f.key] : (config[f.key] || "");
            return React.createElement("div", { key: f.key, className: "mc-config-field" },
              React.createElement("label", { className: "mc-config-label" }, f.label),
              React.createElement("input", {
                className: "mc-config-input",
                type: "text",
                value: val,
                onChange: function (e) { handleChange(f.key, e.target.value); },
                placeholder: f.hint,
              })
            );
          })
        ),

        React.createElement("div", { className: "mc-config-actions" },
          React.createElement("button", {
            className: "mc-btn mc-btn-load",
            onClick: handleLoad,
            disabled: loading,
          }, loading ? "Loading…" : "📋 Load Current"),
          React.createElement("button", {
            className: "mc-btn mc-btn-save",
            onClick: handleSave,
            disabled: saving,
            style: { marginLeft: "0.5rem" },
          }, saving ? "Saving…" : "💾 Save Config"),
          React.createElement("button", {
            className: "mc-btn mc-btn-restart",
            onClick: handleRestart,
            disabled: restarting,
            style: { marginLeft: "0.5rem" },
          }, restarting ? "Restarting…" : "🔄 Restart Gateway")
        ),
        saveMsg && React.createElement("div", {
          className: "mc-config-msg",
          style: { color: saveMsg.indexOf("✅") === 0 ? "var(--color-success, #22c55e)" : "var(--color-danger, #ef4444)" },
        }, saveMsg)
      )
    );
  }

  // ── Register with dashboard ───────────────────────────────────────────
  window.__HERMES_PLUGINS__.register(PLUGIN_NAME, MeshCorePage);
})();
