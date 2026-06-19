/**
 * MeshCore Dashboard Tab — live node status, contacts, and radio stats.
 * Polls /api/plugins/meshcore-platform/status every 5 seconds.
 */
(function () {
  const PLUGIN_NAME = "meshcore-platform";
  const TAB_ID = "meshcore-status";

  // ── Register with dashboard SDK ──────────────────────────────────────
  if (!window.__HERMES_PLUGINS__) {
    console.error("[meshcore] Dashboard SDK not loaded");
    return;
  }

  window.__HERMES_PLUGINS__.register(PLUGIN_NAME, {
    tabs: {
      [TAB_ID]: {
        mount(container) {
          render(container);
          // Poll every 5s
          const interval = setInterval(() => render(container), 5000);
          container._meshcoreInterval = interval;
        },
        unmount(container) {
          if (container._meshcoreInterval) {
            clearInterval(container._meshcoreInterval);
          }
        },
      },
    },
  });

  // ── Render ────────────────────────────────────────────────────────────
  async function render(container) {
    try {
      const resp = await fetch("/api/plugins/meshcore-platform/status");
      const data = await resp.json();
      container.innerHTML = buildHTML(data);
    } catch (e) {
      container.innerHTML = `<div class="card" style="padding:1.5rem;text-align:center;color:var(--color-muted)">
        ⚠️ MeshCore gateway not reachable<br>
        <small>${escapeHtml(String(e))}</small>
      </div>`;
    }
  }

  // ── HTML builder ──────────────────────────────────────────────────────
  function buildHTML(d) {
    const connColor = d.connected ? "var(--color-success, #22c55e)" : "var(--color-danger, #ef4444)";
    const connLabel = d.connected ? "🟢 Connected" : "🔴 Disconnected";

    // Node card
    const node = d.node || {};
    const radio = node.radio;
    const stats = d.stats || {};
    const contacts = d.contacts || {};

    let radioHTML = "";
    if (radio) {
      radioHTML = `
        <div class="stat-row">
          <span class="stat-label">Frequency</span>
          <span class="stat-value">${radio.freq_mhz != null ? radio.freq_mhz + " MHz" : "—"}</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">Bandwidth</span>
          <span class="stat-value">${radio.bw_khz != null ? radio.bw_khz + " kHz" : "—"}</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">SF / CR</span>
          <span class="stat-value">${radio.sf != null ? "SF" + radio.sf : "—"} / ${radio.cr != null ? "4/" + radio.cr : "—"}</span>
        </div>`;
    }

    let batteryHTML = "";
    if (stats.battery_mv != null) {
      const mv = stats.battery_mv;
      const pct = mv > 4200 ? 100 : mv > 4000 ? 90 : mv > 3800 ? 70 : mv > 3600 ? 40 : mv > 3400 ? 15 : 5;
      const batColor = pct > 50 ? "var(--color-success, #22c55e)" : pct > 20 ? "var(--color-warning, #f59e0b)" : "var(--color-danger, #ef4444)";
      batteryHTML = `
        <div class="stat-row">
          <span class="stat-label">Battery</span>
          <span class="stat-value" style="color:${batColor}">${(mv/1000).toFixed(2)}V (~${pct}%)</span>
        </div>`;
    }

    let uptimeHTML = "";
    if (stats.uptime_s != null) {
      const s = stats.uptime_s;
      const h = Math.floor(s / 3600);
      const m = Math.floor((s % 3600) / 60);
      uptimeHTML = `
        <div class="stat-row">
          <span class="stat-label">Uptime</span>
          <span class="stat-value">${h}h ${m}m</span>
        </div>`;
    }

    let packetsHTML = "";
    if (stats.tx_packets != null || stats.rx_packets != null) {
      packetsHTML = `
        <div class="stat-row">
          <span class="stat-label">Packets</span>
          <span class="stat-value">TX: ${stats.tx_packets ?? "—"} / RX: ${stats.rx_packets ?? "—"}</span>
        </div>`;
    }

    let signalHTML = "";
    if (stats.noise != null || stats.rssi != null || stats.snr != null) {
      signalHTML = `
        <div class="stat-row">
          <span class="stat-label">Signal</span>
          <span class="stat-value">Noise: ${stats.noise ?? "—"} | RSSI: ${stats.rssi ?? "—"} | SNR: ${stats.snr != null ? stats.snr + "dB" : "—"}</span>
        </div>`;
    }

    let lastMsgHTML = "";
    if (d.last_message_ago_s != null) {
      const ago = d.last_message_ago_s;
      const agoStr = ago < 60 ? `${Math.round(ago)}s ago` : ago < 3600 ? `${Math.round(ago/60)}m ago` : `${Math.round(ago/3600)}h ago`;
      lastMsgHTML = `
        <div class="stat-row">
          <span class="stat-label">Last Message</span>
          <span class="stat-value">${agoStr}</span>
        </div>`;
    }

    let channelsHTML = "";
    if (d.channels && d.channels.length) {
      channelsHTML = `
        <div class="stat-row">
          <span class="stat-label">Channels</span>
          <span class="stat-value">${d.channels.join(", ")}</span>
        </div>`;
    }

    return `
      <style>
        .mc-grid {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
          gap: 1rem;
          padding: 1rem;
        }
        .mc-card {
          background: var(--color-card, rgba(255,255,255,0.04));
          border: 1px solid var(--color-border, rgba(255,255,255,0.08));
          border-radius: var(--radius-md, 0.5rem);
          padding: 1.25rem;
        }
        .mc-card h3 {
          margin: 0 0 0.75rem 0;
          font-size: 0.9rem;
          text-transform: uppercase;
          letter-spacing: 0.06em;
          color: var(--color-muted);
        }
        .mc-conn-badge {
          display: inline-block;
          padding: 0.2rem 0.6rem;
          border-radius: var(--radius-sm, 0.25rem);
          font-size: 0.8rem;
          font-weight: 600;
          margin-bottom: 0.75rem;
        }
        .stat-row {
          display: flex;
          justify-content: space-between;
          align-items: center;
          padding: 0.35rem 0;
          border-bottom: 1px solid var(--color-border, rgba(255,255,255,0.04));
          font-size: 0.85rem;
        }
        .stat-row:last-child { border-bottom: none; }
        .stat-label { color: var(--color-muted); }
        .stat-value { font-family: var(--font-mono, monospace); font-weight: 500; }
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
        .mc-location {
          font-size: 0.8rem;
          color: var(--color-muted);
          margin-top: 0.25rem;
        }
      </style>

      <div class="mc-grid">
        <!-- Connection -->
        <div class="mc-card">
          <h3>📡 Connection</h3>
          <div class="mc-conn-badge" style="background:${connColor}20;color:${connColor}">${connLabel}</div>
          <div class="stat-row">
            <span class="stat-label">Host</span>
            <span class="stat-value">${escapeHtml(d.host || "—")}:${d.port || "—"}</span>
          </div>
          ${channelsHTML}
          ${lastMsgHTML}
          <div class="stat-row">
            <span class="stat-label">DMs</span>
            <span class="stat-value">${d.dms_enabled ? "✅ Enabled" : "❌ Disabled"}</span>
          </div>
        </div>

        <!-- Node Info -->
        <div class="mc-card">
          <h3>🖥️ Node</h3>
          <div class="stat-row">
            <span class="stat-label">Name</span>
            <span class="stat-value">${escapeHtml(node.name || "unknown")}</span>
          </div>
          <div class="stat-row">
            <span class="stat-label">Pubkey</span>
            <span class="stat-value" style="font-size:0.75rem">${escapeHtml(node.pubkey_prefix || "—")}…</span>
          </div>
          ${node.lat != null ? `
          <div class="stat-row">
            <span class="stat-label">Location</span>
            <span class="stat-value">${node.lat.toFixed(4)}, ${node.lon.toFixed(4)}</span>
          </div>` : ""}
          ${radioHTML}
        </div>

        <!-- Stats -->
        <div class="mc-card">
          <h3>📊 Telemetry</h3>
          ${batteryHTML}
          ${uptimeHTML}
          ${packetsHTML}
          ${signalHTML}
        </div>

        <!-- Contacts -->
        <div class="mc-card">
          <h3>👥 Contacts (${contacts.total || 0})</h3>
          <div class="mc-contacts-summary">
            <div class="mc-contact-chip">🔁 <strong>${contacts.repeaters || 0}</strong> repeaters</div>
            <div class="mc-contact-chip">📱 <strong>${contacts.clients || 0}</strong> clients</div>
            <div class="mc-contact-chip">🏠 <strong>${contacts.rooms || 0}</strong> rooms</div>
          </div>
        </div>
      </div>
    `;
  }

  function escapeHtml(s) {
    const div = document.createElement("div");
    div.textContent = s;
    return div.innerHTML;
  }
})();
