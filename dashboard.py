#!/usr/bin/env python3
"""
Polypanic Dashboard — local web UI for controlling the paper trader.
Run with: ./run.sh   (no args)
Then open: http://localhost:5000
"""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template_string, request

app = Flask(__name__)
BASE = Path(__file__).parent

BOTS = [
    {
        "id":          "main",
        "name":        "Both Sides",
        "db":          "polymarket_observer.db",
        "pid_file":    ".observer_main.pid",
        "config_file": "config.json",
        "only_side":   "",
    },
    {
        "id":          "down",
        "name":        "Down Only",
        "db":          "polymarket_down.db",
        "pid_file":    ".observer_down.pid",
        "config_file": "config_down.json",
        "only_side":   "down",
    },
]

DEFAULTS = {
    "entry_threshold": 0.40,
    "exit_threshold":  0.65,
    "stop_loss":       0.10,
    "stop_loss_after": 60,
    "entry_delay":     5,
    "btc_momentum":    30.0,
    "bankroll":        1000.0,
    "poll":            3.0,
}

HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Polypanic</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           background: #0a0a0a; color: #e0e0e0; padding: 24px; }
    h1 { font-size: 1.3rem; font-weight: 600; margin-bottom: 20px; color: #fff; }
    .section-label { font-size: 0.68rem; font-weight: 600; text-transform: uppercase;
                     letter-spacing: 0.1em; color: #444; margin-bottom: 10px;
                     display: flex; align-items: center; gap: 8px; }
    .section-label::after { content: ""; flex: 1; height: 1px; background: #222; }

    .bots { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }

    .bot { background: #111; border: 1px solid #222; border-radius: 10px;
           padding: 20px; display: flex; flex-direction: column; gap: 16px; }
    .bot.running { border-color: #22c55e33; }

    /* Header */
    .bot-header { display: flex; align-items: center; gap: 10px; }
    .dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
    .dot.running { background: #22c55e; box-shadow: 0 0 6px #22c55e88; }
    .dot.stopped { background: #333; }
    .bot-name { font-weight: 600; color: #fff; font-size: 0.95rem; }
    .bot-bankroll { margin-left: auto; font-size: 1.05rem; font-weight: 700; }
    .up   { color: #22c55e; }
    .down { color: #ef4444; }
    .flat { color: #666; }

    /* Stats */
    .stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; }
    .stat { background: #0d0d0d; border: 1px solid #1e1e1e; border-radius: 7px; padding: 9px 11px; }
    .stat-label { font-size: 0.65rem; color: #444; text-transform: uppercase;
                  letter-spacing: 0.06em; margin-bottom: 3px; }
    .stat-value { font-size: 0.95rem; font-weight: 700; color: #fff; }

    /* Live feed */
    .feed-wrap { background: #000; border: 1px solid #1a1a1a; border-radius: 7px;
                 overflow: hidden; }
    .feed-header { display: flex; align-items: center; justify-content: space-between;
                   padding: 7px 12px; border-bottom: 1px solid #1a1a1a;
                   background: #0d0d0d; }
    .feed-market { font-family: monospace; font-size: 0.72rem; color: #555; }
    .feed-window { font-family: monospace; font-size: 0.72rem; }
    .feed-body { height: 240px; overflow-y: auto; padding: 8px 0;
                 display: flex; flex-direction: column-reverse; }
    .tick { display: flex; gap: 10px; align-items: baseline;
            padding: 2px 12px; font-family: "SF Mono", "Fira Code", monospace;
            font-size: 0.74rem; line-height: 1.6; white-space: nowrap; }
    .tick:hover { background: #0f0f0f; }
    .tick-time { color: #333; min-width: 58px; }
    .tick-secs { min-width: 44px; }
    .secs-ok { color: #666; }
    .secs-warn { color: #f59e0b; }
    .secs-danger { color: #ef4444; }
    .tick-btc { color: #888; min-width: 130px; }
    .btc-up { color: #22c55e; }
    .btc-dn { color: #ef4444; }
    .tick-divider { color: #2a2a2a; }
    .tick-side { display: flex; gap: 6px; align-items: baseline; }
    .label-up { color: #60a5fa; font-weight: 700; min-width: 24px; }
    .label-dn { color: #f472b6; font-weight: 700; min-width: 36px; }
    .ask-buy  { color: #22c55e; font-weight: 700; }
    .ask-norm { color: #aaa; }
    .bid-val  { color: #555; }
    .pos-dot  { color: #facc15; }
    .no-ticks { padding: 20px 12px; font-family: monospace; font-size: 0.75rem; color: #333; }

    /* Controls (collapsible) */
    details { }
    summary { cursor: pointer; user-select: none; list-style: none;
              display: flex; align-items: center; gap: 8px; }
    summary::-webkit-details-marker { display: none; }
    .chevron { color: #444; font-size: 0.7rem; transition: transform 0.2s; }
    details[open] .chevron { transform: rotate(90deg); }

    .field { margin-bottom: 12px; }
    label { display: block; font-size: 0.78rem; color: #666; margin-bottom: 5px; }
    label span { float: right; color: #bbb; font-weight: 600; }
    input[type=range] { width: 100%; accent-color: #6366f1; cursor: pointer; }
    input[type=number] { width: 100%; background: #0d0d0d; border: 1px solid #2a2a2a;
                         border-radius: 6px; color: #e0e0e0; padding: 7px 10px;
                         font-size: 0.85rem; }
    input[type=number]:focus { outline: none; border-color: #6366f1; }

    .actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 14px; }
    .btn { padding: 8px; border-radius: 7px; border: none; font-size: 0.82rem;
           font-weight: 600; cursor: pointer; transition: opacity 0.15s; text-align: center; }
    .btn:hover:not(:disabled) { opacity: 0.8; }
    .btn:disabled { opacity: 0.25; cursor: default; }
    .btn-save  { background: #6366f1; color: #fff; grid-column: span 2; }
    .btn-start { background: #22c55e; color: #000; }
    .btn-stop  { background: #ef4444; color: #fff; }

    /* Trades */
    table { width: 100%; border-collapse: collapse; font-size: 0.76rem; }
    th { text-align: left; padding: 5px 8px; color: #444; font-weight: 500;
         border-bottom: 1px solid #1e1e1e; white-space: nowrap; }
    td { padding: 6px 8px; border-bottom: 1px solid #161616; }
    tr:last-child td { border-bottom: none; }
    .side-up { color: #60a5fa; font-weight: 600; }
    .side-dn { color: #f472b6; font-weight: 600; }
    .pnl-pos { color: #22c55e; font-weight: 600; }
    .pnl-neg { color: #ef4444; font-weight: 600; }
    .reason  { font-size: 0.68rem; color: #444; }
  </style>
</head>
<body>
  <h1>Polypanic</h1>

  <div class="bots">
  {% for bot in bots %}
    <div class="bot {{ 'running' if bot.running }}" id="card-{{ bot.id }}">

      <!-- Header -->
      <div class="bot-header">
        <div class="dot {{ 'running' if bot.running else 'stopped' }}" id="dot-{{ bot.id }}"></div>
        <span class="bot-name">{{ bot.name }}</span>
        <span class="bot-bankroll {{ bot.bankroll_class }}" id="bankroll-{{ bot.id }}">
          {{ bot.bankroll_str }}
        </span>
      </div>

      <!-- Stats -->
      <div class="stats" id="stats-{{ bot.id }}">
        <div class="stat">
          <div class="stat-label">Total P&L</div>
          <div class="stat-value {{ 'up' if bot.stats.total_pnl >= 0 else 'down' }}">
            {{ '%+.2f'|format(bot.stats.total_pnl) }}
          </div>
        </div>
        <div class="stat">
          <div class="stat-label">Win rate</div>
          <div class="stat-value">{{ bot.stats.win_rate }}%</div>
        </div>
        <div class="stat">
          <div class="stat-label">Trades</div>
          <div class="stat-value">{{ bot.stats.total_sells }}</div>
        </div>
        <div class="stat">
          <div class="stat-label">exit_target</div>
          <div class="stat-value up">{{ '%+.2f'|format(bot.stats.exit_target_pnl) }}</div>
        </div>
        <div class="stat">
          <div class="stat-label">force_exit</div>
          <div class="stat-value down">{{ '%+.2f'|format(bot.stats.force_exit_pnl) }}</div>
        </div>
        <div class="stat">
          <div class="stat-label">stop_loss</div>
          <div class="stat-value down">{{ '%+.2f'|format(bot.stats.stop_loss_pnl) }}</div>
        </div>
      </div>

      <!-- Live feed -->
      <div>
        <div class="section-label">Live feed</div>
        <div class="feed-wrap">
          <div class="feed-header">
            <span class="feed-market" id="feed-slug-{{ bot.id }}">—</span>
            <span class="feed-window" id="feed-window-{{ bot.id }}"></span>
          </div>
          <div class="feed-body" id="feed-{{ bot.id }}">
            <div class="no-ticks">Waiting for tick data…</div>
          </div>
        </div>
      </div>

      <!-- Controls (collapsible) -->
      <div>
        <details>
          <summary>
            <div class="section-label" style="flex:1; margin:0;">
              Controls <span class="chevron">▶</span>
            </div>
          </summary>
          <div style="margin-top:12px;">
            <form method="POST" action="/save/{{ bot.id }}">
              <div class="field">
                <label>Entry threshold
                  <span id="ev_{{ bot.id }}">≤ ${{ '%.2f'|format(bot.cfg.entry_threshold) }}</span>
                </label>
                <input type="range" name="entry_threshold" min="0.20" max="0.50" step="0.01"
                       value="{{ bot.cfg.entry_threshold }}"
                       oninput="document.getElementById('ev_{{ bot.id }}').textContent='≤ $'+parseFloat(this.value).toFixed(2)">
              </div>
              <div class="field">
                <label>Exit threshold
                  <span id="xv_{{ bot.id }}">≥ ${{ '%.2f'|format(bot.cfg.exit_threshold) }}</span>
                </label>
                <input type="range" name="exit_threshold" min="0.50" max="0.90" step="0.01"
                       value="{{ bot.cfg.exit_threshold }}"
                       oninput="document.getElementById('xv_{{ bot.id }}').textContent='≥ $'+parseFloat(this.value).toFixed(2)">
              </div>
              <div class="field">
                <label>Stop-loss price
                  <span id="slv_{{ bot.id }}">${{ '%.2f'|format(bot.cfg.stop_loss) }}</span>
                </label>
                <input type="range" name="stop_loss" min="0.00" max="0.30" step="0.01"
                       value="{{ bot.cfg.stop_loss }}"
                       oninput="document.getElementById('slv_{{ bot.id }}').textContent='$'+parseFloat(this.value).toFixed(2)">
              </div>
              <div class="field">
                <label>Stop-loss after
                  <span id="sav_{{ bot.id }}">{{ bot.cfg.stop_loss_after }}s remaining</span>
                </label>
                <input type="range" name="stop_loss_after" min="0" max="180" step="5"
                       value="{{ bot.cfg.stop_loss_after }}"
                       oninput="document.getElementById('sav_{{ bot.id }}').textContent=this.value+'s remaining'">
              </div>
              <div class="field">
                <label>Entry delay
                  <span id="edv_{{ bot.id }}">{{ bot.cfg.entry_delay }}s</span>
                </label>
                <input type="range" name="entry_delay" min="0" max="30" step="1"
                       value="{{ bot.cfg.entry_delay }}"
                       oninput="document.getElementById('edv_{{ bot.id }}').textContent=this.value+'s'">
              </div>
              <div class="field">
                <label>BTC momentum filter
                  <span id="bmv_{{ bot.id }}">${{ bot.cfg.btc_momentum|int }}</span>
                </label>
                <input type="range" name="btc_momentum" min="0" max="100" step="5"
                       value="{{ bot.cfg.btc_momentum }}"
                       oninput="document.getElementById('bmv_{{ bot.id }}').textContent='$'+this.value">
              </div>
              <div class="field">
                <label>Starting bankroll</label>
                <input type="number" name="bankroll" value="{{ bot.cfg.bankroll }}" step="50" min="100">
              </div>

              <div class="actions">
                <button type="submit" class="btn btn-save">Save</button>
                <button type="submit" form="start_{{ bot.id }}" class="btn btn-start"
                        {{ 'disabled' if bot.running }}>▶ Start</button>
                <button type="submit" form="stop_{{ bot.id }}"  class="btn btn-stop"
                        {{ 'disabled' if not bot.running }}>■ Stop</button>
              </div>
            </form>

            <form id="start_{{ bot.id }}" method="POST" action="/start/{{ bot.id }}"></form>
            <form id="stop_{{ bot.id }}"  method="POST" action="/stop/{{ bot.id }}"></form>
          </div>
        </details>
      </div>

      <!-- Recent trades -->
      <div>
        <div class="section-label">Recent trades</div>
        <div id="trades-{{ bot.id }}">
          {% if bot.trades %}
          <table>
            <thead>
              <tr><th>Time</th><th>Side</th><th>Action</th><th>Price</th><th>P&L</th><th>Bankroll</th></tr>
            </thead>
            <tbody>
              {% for t in bot.trades %}
              <tr>
                <td style="color:#333">{{ t.time }}</td>
                <td class="{{ 'side-up' if t.side == 'up' else 'side-dn' }}">{{ t.side.upper() }}</td>
                <td>{{ t.action }}<div class="reason">{{ t.reason }}</div></td>
                <td>${{ '%.3f'|format(t.price) }}</td>
                <td class="{{ 'pnl-pos' if t.pnl > 0 else ('pnl-neg' if t.pnl < 0 else '') }}">
                  {% if t.action == 'sell' %}{{ '%+.2f'|format(t.pnl) }}{% endif %}
                </td>
                <td>${{ '%.2f'|format(t.bankroll_after) }}</td>
              </tr>
              {% endfor %}
            </tbody>
          </table>
          {% else %}
            <p style="color:#333; font-size:0.82rem;">No trades yet.</p>
          {% endif %}
        </div>
      </div>

    </div>
  {% endfor %}
  </div>

  <script>
    // Per-bot config (entry threshold for coloring)
    const botCfg = {
      {% for bot in bots %}
      "{{ bot.id }}": { entry: {{ bot.cfg.entry_threshold }}, exit: {{ bot.cfg.exit_threshold }} },
      {% endfor %}
    };

    function fmtBtc(v) {
      if (v == null) return "—";
      return "$" + Math.round(v).toLocaleString();
    }

    function renderTick(t, cfg, positions) {
      const holdUp = positions.some(p => p.side === "up");
      const holdDn = positions.some(p => p.side === "down");

      // Seconds remaining styling
      let secsClass = "tick-secs secs-ok";
      if (t.secs == null)    secsClass = "tick-secs secs-ok";
      else if (t.secs <= 30) secsClass = "tick-secs secs-danger";
      else if (t.secs <= 60) secsClass = "tick-secs secs-warn";

      // BTC delta
      let btcDeltaHtml = "";
      if (t.btc_delta != null) {
        const sign = t.btc_delta >= 0 ? "+" : "";
        const cls  = t.btc_delta >= 0 ? "btc-up" : "btc-dn";
        btcDeltaHtml = ` <span class="${cls}">${sign}$${Math.round(Math.abs(t.btc_delta))}</span>`;
      }

      // UP side
      const upAskCls = (t.up_ask != null && t.up_ask <= cfg.entry) ? "ask-buy" : "ask-norm";
      const upPos    = holdUp ? '<span class="pos-dot">●</span> ' : '';
      const upAsk    = t.up_ask  != null ? t.up_ask.toFixed(3)  : "—";
      const upBid    = t.up_bid  != null ? t.up_bid.toFixed(3)  : "—";

      // DOWN side
      const dnAskCls = (t.dn_ask != null && t.dn_ask <= cfg.entry) ? "ask-buy" : "ask-norm";
      const dnPos    = holdDn ? '<span class="pos-dot">●</span> ' : '';
      const dnAsk    = t.dn_ask  != null ? t.dn_ask.toFixed(3)  : "—";
      const dnBid    = t.dn_bid  != null ? t.dn_bid.toFixed(3)  : "—";

      return `<div class="tick">
        <span class="tick-time">${t.time}</span>
        <span class="${secsClass}">[${t.secs != null ? t.secs : "?"}s]</span>
        <span class="tick-btc">${fmtBtc(t.btc)}${btcDeltaHtml}</span>
        <span class="tick-divider">│</span>
        <span class="tick-side">
          ${upPos}<span class="label-up">UP</span>
          <span class="${upAskCls}">a:${upAsk}</span>
          <span class="bid-val">b:${upBid}</span>
        </span>
        <span class="tick-divider">│</span>
        <span class="tick-side">
          ${dnPos}<span class="label-dn">DOWN</span>
          <span class="${dnAskCls}">a:${dnAsk}</span>
          <span class="bid-val">b:${dnBid}</span>
        </span>
      </div>`;
    }

    function updateFeed(botId) {
      fetch(`/ticks/${botId}`)
        .then(r => r.json())
        .then(data => {
          const cfg = botCfg[botId];
          const feed = document.getElementById(`feed-${botId}`);
          const slugEl = document.getElementById(`feed-slug-${botId}`);
          const winEl  = document.getElementById(`feed-window-${botId}`);

          if (data.market) {
            slugEl.textContent = data.market.slug;
            if (data.market.end_ts) {
              const secsLeft = Math.max(0, data.market.end_ts - Math.floor(Date.now() / 1000));
              winEl.textContent = secsLeft + "s left";
              const cls = secsLeft <= 30 ? "secs-danger" : secsLeft <= 60 ? "secs-warn" : "secs-ok";
              winEl.className = "feed-window " + cls;
            }
          }

          if (!data.ticks || data.ticks.length === 0) return;

          // ticks come newest-first from the API; feed is column-reverse so render in that order
          feed.innerHTML = data.ticks
            .map(t => renderTick(t, cfg, data.positions))
            .join("");
        })
        .catch(() => {});
    }

    function updateStats(botId) {
      fetch(`/stats/${botId}`)
        .then(r => r.json())
        .then(data => {
          // Update bankroll
          const bEl = document.getElementById(`bankroll-${botId}`);
          if (bEl && data.bankroll != null) {
            const diff = data.bankroll - data.starting_bankroll;
            bEl.textContent = "$" + data.bankroll.toLocaleString("en-US", {minimumFractionDigits:2, maximumFractionDigits:2});
            bEl.className = "bot-bankroll " + (diff > 0 ? "up" : diff < 0 ? "down" : "flat");
          }

          // Update stats grid
          const s = data.stats;
          if (s) {
            const sEl = document.getElementById(`stats-${botId}`);
            if (sEl) {
              const pnlCls = s.total_pnl >= 0 ? "up" : "down";
              const sign = v => (v >= 0 ? "+" : "") + v.toFixed(2);
              sEl.innerHTML = `
                <div class="stat"><div class="stat-label">Total P&L</div>
                  <div class="stat-value ${pnlCls}">${sign(s.total_pnl)}</div></div>
                <div class="stat"><div class="stat-label">Win rate</div>
                  <div class="stat-value">${s.win_rate}%</div></div>
                <div class="stat"><div class="stat-label">Trades</div>
                  <div class="stat-value">${s.total_sells}</div></div>
                <div class="stat"><div class="stat-label">exit_target</div>
                  <div class="stat-value up">${sign(s.exit_target_pnl)}</div></div>
                <div class="stat"><div class="stat-label">force_exit</div>
                  <div class="stat-value down">${sign(s.force_exit_pnl)}</div></div>
                <div class="stat"><div class="stat-label">stop_loss</div>
                  <div class="stat-value down">${sign(s.stop_loss_pnl)}</div></div>`;
            }
          }

          // Update trades table
          const tEl = document.getElementById(`trades-${botId}`);
          if (tEl && data.trades) {
            if (data.trades.length === 0) {
              tEl.innerHTML = '<p style="color:#333; font-size:0.82rem;">No trades yet.</p>';
            } else {
              const rows = data.trades.map(t => {
                const sideCls = t.side === "up" ? "side-up" : "side-dn";
                const pnlCls  = t.pnl > 0 ? "pnl-pos" : t.pnl < 0 ? "pnl-neg" : "";
                const pnlStr  = t.action === "sell" ? (t.pnl >= 0 ? "+" : "") + t.pnl.toFixed(2) : "";
                return `<tr>
                  <td style="color:#333">${t.time}</td>
                  <td class="${sideCls}">${t.side.toUpperCase()}</td>
                  <td>${t.action}<div class="reason">${t.reason || ""}</div></td>
                  <td>$${t.price.toFixed(3)}</td>
                  <td class="${pnlCls}">${pnlStr}</td>
                  <td>$${t.bankroll_after.toFixed(2)}</td>
                </tr>`;
              }).join("");
              tEl.innerHTML = `<table>
                <thead><tr><th>Time</th><th>Side</th><th>Action</th><th>Price</th><th>P&L</th><th>Bankroll</th></tr></thead>
                <tbody>${rows}</tbody>
              </table>`;
            }
          }
        })
        .catch(() => {});
    }

    const BOT_IDS = [{% for bot in bots %}"{{ bot.id }}", {% endfor %}];

    // Tick feed: poll every 3s
    BOT_IDS.forEach(id => {
      updateFeed(id);
      setInterval(() => updateFeed(id), 3000);
    });

    // Stats + trades: poll every 5s
    BOT_IDS.forEach(id => {
      setInterval(() => updateStats(id), 5000);
    });
  </script>
</body>
</html>
"""


def load_config(bot):
    path = BASE / bot["config_file"]
    if path.exists():
        return {**DEFAULTS, **json.loads(path.read_text())}
    return dict(DEFAULTS)


def save_config(bot, data):
    (BASE / bot["config_file"]).write_text(json.dumps(data, indent=2))


def get_pid(bot):
    pid_path = BASE / bot["pid_file"]
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)
            return pid
        except (ProcessLookupError, ValueError, OSError):
            pid_path.unlink(missing_ok=True)
    return None


def get_db_stats(bot):
    db_path = BASE / bot["db"]
    empty_stats = {
        "total_pnl": 0, "win_rate": 0, "total_sells": 0,
        "exit_target_pnl": 0, "force_exit_pnl": 0, "stop_loss_pnl": 0,
    }
    if not db_path.exists():
        return [], empty_stats, None

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    trades = conn.execute("""
        SELECT side, action, reason,
               ROUND(price, 3)          AS price,
               ROUND(pnl, 2)            AS pnl,
               ROUND(bankroll_after, 2) AS bankroll_after,
               strftime('%H:%M:%S', datetime(timestamp, 'unixepoch')) AS time
        FROM paper_trades ORDER BY id DESC LIMIT 20
    """).fetchall()

    row = conn.execute("""
        SELECT
            SUM(CASE WHEN action='sell' THEN pnl ELSE 0 END) AS total_pnl,
            SUM(CASE WHEN action='sell' AND pnl > 0 THEN 1 ELSE 0 END) * 100.0
                / MAX(SUM(CASE WHEN action='sell' THEN 1 ELSE 0 END), 1) AS win_rate,
            SUM(CASE WHEN action='sell' THEN 1 ELSE 0 END)               AS total_sells,
            SUM(CASE WHEN reason='exit_target' THEN pnl ELSE 0 END)      AS exit_target_pnl,
            SUM(CASE WHEN reason='force_exit'  THEN pnl ELSE 0 END)      AS force_exit_pnl,
            SUM(CASE WHEN reason='stop_loss'   THEN pnl ELSE 0 END)      AS stop_loss_pnl
        FROM paper_trades
    """).fetchone()

    last = conn.execute(
        "SELECT bankroll_after FROM paper_trades ORDER BY id DESC LIMIT 1"
    ).fetchone()

    conn.close()

    stats = {
        "total_pnl":       round(row["total_pnl"] or 0, 2),
        "win_rate":        round(row["win_rate"] or 0, 1),
        "total_sells":     row["total_sells"] or 0,
        "exit_target_pnl": round(row["exit_target_pnl"] or 0, 2),
        "force_exit_pnl":  round(row["force_exit_pnl"] or 0, 2),
        "stop_loss_pnl":   round(row["stop_loss_pnl"] or 0, 2),
    }
    bankroll = round(last["bankroll_after"], 2) if last else None
    return [dict(t) for t in trades], stats, bankroll


def build_bot_context(bot):
    cfg                      = load_config(bot)
    pid                      = get_pid(bot)
    trades, stats, bankroll  = get_db_stats(bot)

    if bankroll is None:
        bankroll_str   = "$—"
        bankroll_class = "flat"
    else:
        diff           = bankroll - cfg["bankroll"]
        bankroll_str   = f"${bankroll:,.2f}"
        bankroll_class = "up" if diff > 0 else ("down" if diff < 0 else "flat")

    return {
        **bot,
        "cfg":            type("cfg", (), cfg)(),
        "running":        pid is not None,
        "pid":            pid,
        "bankroll_str":   bankroll_str,
        "bankroll_class": bankroll_class,
        "trades":         trades,
        "stats":          stats,
    }


@app.route("/")
def index():
    return render_template_string(HTML, bots=[build_bot_context(b) for b in BOTS])


@app.route("/ticks/<bot_id>")
def ticks(bot_id):
    bot = next((b for b in BOTS if b["id"] == bot_id), None)
    if bot is None:
        return jsonify({"ticks": [], "positions": [], "market": None})

    db_path = BASE / bot["db"]
    if not db_path.exists():
        return jsonify({"ticks": [], "positions": [], "market": None})

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    market = conn.execute("""
        SELECT slug, window_start_ts, window_end_ts
        FROM markets ORDER BY window_start_ts DESC LIMIT 1
    """).fetchone()

    if not market:
        conn.close()
        return jsonify({"ticks": [], "positions": [], "market": None})

    slug = market["slug"]

    tick_rows = conn.execute("""
        SELECT timestamp, seconds_remaining,
               up_best_ask, up_best_bid,
               down_best_ask, down_best_bid,
               btc_spot_price, btc_delta_from_open, price_source
        FROM price_ticks WHERE slug = ?
        ORDER BY timestamp DESC LIMIT 50
    """, (slug,)).fetchall()

    # Open positions: buys in this window without a later sell on the same side
    pos_rows = conn.execute("""
        SELECT side FROM paper_trades b
        WHERE b.slug = ? AND b.action = 'buy'
          AND NOT EXISTS (
              SELECT 1 FROM paper_trades s
              WHERE s.slug = b.slug AND s.side = b.side
                AND s.action = 'sell' AND s.timestamp > b.timestamp
          )
    """, (slug,)).fetchall()

    conn.close()

    def fmt_ts(ts):
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")
        except Exception:
            return "—"

    ticks_data = [
        {
            "time":      fmt_ts(t["timestamp"]),
            "secs":      int(t["seconds_remaining"]) if t["seconds_remaining"] is not None else None,
            "up_ask":    t["up_best_ask"],
            "up_bid":    t["up_best_bid"],
            "dn_ask":    t["down_best_ask"],
            "dn_bid":    t["down_best_bid"],
            "btc":       t["btc_spot_price"],
            "btc_delta": t["btc_delta_from_open"],
            "src":       t["price_source"],
        }
        for t in tick_rows
    ]

    positions = [{"side": r["side"]} for r in pos_rows]

    return jsonify({
        "ticks":     ticks_data,
        "positions": positions,
        "market": {
            "slug":   slug,
            "end_ts": market["window_end_ts"],
        },
    })


@app.route("/stats/<bot_id>")
def stats_api(bot_id):
    bot = next((b for b in BOTS if b["id"] == bot_id), None)
    if bot is None:
        return jsonify({})
    cfg = load_config(bot)
    trades, stats, bankroll = get_db_stats(bot)
    return jsonify({
        "stats":             stats,
        "bankroll":          bankroll,
        "starting_bankroll": cfg["bankroll"],
        "trades":            trades,
    })


@app.route("/save/<bot_id>", methods=["POST"])
def save(bot_id):
    bot = next(b for b in BOTS if b["id"] == bot_id)
    cfg = {
        "entry_threshold": float(request.form["entry_threshold"]),
        "exit_threshold":  float(request.form["exit_threshold"]),
        "stop_loss":       float(request.form["stop_loss"]),
        "stop_loss_after": int(request.form["stop_loss_after"]),
        "entry_delay":     int(request.form["entry_delay"]),
        "btc_momentum":    float(request.form["btc_momentum"]),
        "bankroll":        float(request.form["bankroll"]),
        "poll":            load_config(bot).get("poll", 3.0),
    }
    save_config(bot, cfg)
    return redirect("/")


@app.route("/start/<bot_id>", methods=["POST"])
def start(bot_id):
    bot = next(b for b in BOTS if b["id"] == bot_id)
    if get_pid(bot):
        return redirect("/")

    cfg = load_config(bot)
    cmd = [
        sys.executable, "observer.py",
        "--entry",           str(cfg["entry_threshold"]),
        "--exit",            str(cfg["exit_threshold"]),
        "--stop-loss",       str(cfg["stop_loss"]),
        "--stop-loss-after", str(cfg["stop_loss_after"]),
        "--entry-delay",     str(cfg["entry_delay"]),
        "--btc-momentum",    str(cfg["btc_momentum"]),
        "--bankroll",        str(cfg["bankroll"]),
        "--poll",            str(cfg["poll"]),
        "--db",              bot["db"],
    ]
    if bot["only_side"]:
        cmd += ["--only-side", bot["only_side"]]

    proc = subprocess.Popen(cmd, cwd=BASE)
    (BASE / bot["pid_file"]).write_text(str(proc.pid))
    return redirect("/")


@app.route("/stop/<bot_id>", methods=["POST"])
def stop(bot_id):
    bot = next(b for b in BOTS if b["id"] == bot_id)
    pid = get_pid(bot)
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(10):
                time.sleep(0.5)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        except ProcessLookupError:
            pass
        (BASE / bot["pid_file"]).unlink(missing_ok=True)
    return redirect("/")


if __name__ == "__main__":
    print("Dashboard at http://localhost:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)
