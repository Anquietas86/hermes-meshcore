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
      `),

      // ── Connection card ────────────────────────────────────────────
      React.createElement(C.Card, null,
        React.createElement(C.CardContent, null,
          React.createElement("h3", { style: { margin: "0 0 0.75rem 0", fontSize: "0.9rem", textTransform: "uppercase", letterSpacing: "0.06em", color: "var(--color-muted)" } }, "📡 Connection"),
          React.createElement("div", { className: "mc-conn-badge", style: { background: connColor + "20", color: connColor } }, connLabel),
          React.createElement(StatRow, { label: "Host", value: escapeHtml(data.host) + ":" + (data.port || "—"), mono: true }),
          data.channels && data.channels.length > 0 && React.createElement(StatRow, { label: "Channels", value: data.channels.join(", "), mono: true }),
          React.createElement(StatRow, { label: "Last Message", value: agoStr(data.last_message_ago_s) }),
          React.createElement(StatRow, { label: "DMs", value: data.dms_enabled ? "✅ Enabled" : "❌ Disabled" })
        )
      ),

      // ── Node Info card ─────────────────────────────────────────────
      React.createElement(C.Card, null,
        React.createElement(C.CardContent, null,
          React.createElement("h3", { style: { margin: "0 0 0.75rem 0", fontSize: "0.9rem", textTransform: "uppercase", letterSpacing: "0.06em", color: "var(--color-muted)" } }, "🖥️ Node"),
          React.createElement(StatRow, { label: "Name", value: escapeHtml(node.name || "unknown") }),
          React.createElement(StatRow, { label: "Pubkey", value: escapeHtml(node.pubkey_prefix || "—") + "…", mono: true }),
          node.lat != null && React.createElement(StatRow, { label: "Location", value: node.lat.toFixed(4) + ", " + node.lon.toFixed(4), mono: true }),
          radio && React.createElement(React.Fragment, null,
            React.createElement(StatRow, { label: "Frequency", value: radio.freq_mhz != null ? radio.freq_mhz + " MHz" : "—", mono: true }),
            React.createElement(StatRow, { label: "Bandwidth", value: radio.bw_khz != null ? radio.bw_khz + " kHz" : "—", mono: true }),
            React.createElement(StatRow, { label: "SF / CR", value: radio.sf != null ? "SF" + radio.sf + " / 4/" + (radio.cr || "—") : "—", mono: true })
          )
        )
      ),

      // ── Telemetry card ─────────────────────────────────────────────
      React.createElement(C.Card, null,
        React.createElement(C.CardContent, null,
          React.createElement("h3", { style: { margin: "0 0 0.75rem 0", fontSize: "0.9rem", textTransform: "uppercase", letterSpacing: "0.06em", color: "var(--color-muted)" } }, "📊 Telemetry"),
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
      ),

      // ── Contacts card ──────────────────────────────────────────────
      React.createElement(C.Card, null,
        React.createElement(C.CardContent, null,
          React.createElement("h3", { style: { margin: "0 0 0.75rem 0", fontSize: "0.9rem", textTransform: "uppercase", letterSpacing: "0.06em", color: "var(--color-muted)" } }, "👥 Contacts (" + (contacts.total || 0) + ")"),
          React.createElement("div", { className: "mc-contacts-summary" },
            React.createElement("div", { className: "mc-contact-chip" }, "🔁 ", React.createElement("strong", null, contacts.repeaters || 0), " repeaters"),
            React.createElement("div", { className: "mc-contact-chip" }, "📱 ", React.createElement("strong", null, contacts.clients || 0), " clients"),
            React.createElement("div", { className: "mc-contact-chip" }, "🏠 ", React.createElement("strong", null, contacts.rooms || 0), " rooms")
          )
        )
      ),

      // ── Channels card ──────────────────────────────────────────────
      data.channels && data.channels.length > 0 && React.createElement(C.Card, null,
        React.createElement(C.CardContent, null,
          React.createElement("h3", { style: { margin: "0 0 0.75rem 0", fontSize: "0.9rem", textTransform: "uppercase", letterSpacing: "0.06em", color: "var(--color-muted)" } }, "📢 Channels (" + data.channels.length + ")"),
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
      ),

      // ── Admin card ─────────────────────────────────────────────────
      (function () {
        var admin = data.admin || {};
        var hasContent = (admin.nodes && admin.nodes.length > 0) ||
                         (admin.channels && admin.channels.length > 0) ||
                         (admin.require_mention_channels && admin.require_mention_channels.length > 0);
        if (!hasContent) return null;

        return React.createElement(C.Card, null,
          React.createElement(C.CardContent, null,
            React.createElement("h3", { style: { margin: "0 0 0.75rem 0", fontSize: "0.9rem", textTransform: "uppercase", letterSpacing: "0.06em", color: "var(--color-muted)" } }, "🔐 Admin Settings"),

            admin.nodes && admin.nodes.length > 0 && React.createElement("div", { style: { marginBottom: "0.75rem" } },
              React.createElement("div", { style: { fontSize: "0.8rem", color: "var(--color-muted)", marginBottom: "0.25rem" } }, "Authorised Admin Nodes"),
              React.createElement("div", { className: "mc-tag-list" },
                admin.nodes.map(function (n) {
                  return React.createElement("span", { key: n, className: "mc-tag mc-tag-admin" }, n);
                })
              )
            ),

            admin.channels && admin.channels.length > 0 && React.createElement("div", { style: { marginBottom: "0.75rem" } },
              React.createElement("div", { style: { fontSize: "0.8rem", color: "var(--color-muted)", marginBottom: "0.25rem" } }, "Admin Channels (trusted, no mention required)"),
              React.createElement("div", { className: "mc-tag-list" },
                admin.channels.map(function (c) {
                  return React.createElement("span", { key: c, className: "mc-tag mc-tag-channel" }, "ch " + c);
                })
              )
            ),

            admin.require_mention_channels && admin.require_mention_channels.length > 0 && React.createElement("div", null,
              React.createElement("div", { style: { fontSize: "0.8rem", color: "var(--color-muted)", marginBottom: "0.25rem" } }, "Mention-Gated Channels (@mention required)"),
              React.createElement("div", { className: "mc-tag-list" },
                admin.require_mention_channels.map(function (c) {
                  return React.createElement("span", { key: c, className: "mc-tag mc-tag-mention" }, "ch " + c);
                })
              )
            )
          )
        );
      })()
    );
  }

  // ── Register with dashboard ───────────────────────────────────────────
  window.__HERMES_PLUGINS__.register(PLUGIN_NAME, MeshCorePage);
})();
