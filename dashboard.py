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
from pathlib import Path

from flask import Flask, redirect, render_template_string, request

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
           background: #0f0f0f; color: #e0e0e0; padding: 32px; }
    h1 { font-size: 1.4rem; font-weight: 600; margin-bottom: 24px; color: #fff; }
    h2 { font-size: 0.8rem; font-weight: 600; text-transform: uppercase;
         letter-spacing: 0.08em; color: #666; margin-bottom: 14px; }

    .bots { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }

    .bot { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 10px; padding: 22px; }
    .bot.running { border-color: #22c55e44; }

    .bot-header { display: flex; align-items: center; gap: 10px; margin-bottom: 18px; }
    .dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
    .dot.running { background: #22c55e; box-shadow: 0 0 7px #22c55e88; }
    .dot.stopped { background: #444; }
    .bot-name { font-weight: 600; color: #fff; font-size: 1rem; }
    .bot-bankroll { margin-left: auto; font-size: 1.05rem; font-weight: 700; }
    .up   { color: #22c55e; }
    .down { color: #ef4444; }
    .flat { color: #888; }

    /* Stats */
    .stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-bottom: 18px; }
    .stat { background: #111; border: 1px solid #222; border-radius: 7px; padding: 10px 12px; }
    .stat-label { font-size: 0.68rem; color: #555; text-transform: uppercase;
                  letter-spacing: 0.06em; margin-bottom: 3px; }
    .stat-value { font-size: 1rem; font-weight: 700; color: #fff; }

    /* Form */
    .field { margin-bottom: 14px; }
    label { display: block; font-size: 0.78rem; color: #777; margin-bottom: 5px; }
    label span { float: right; color: #bbb; font-weight: 600; }
    input[type=range] { width: 100%; accent-color: #6366f1; cursor: pointer; }
    input[type=number] { width: 100%; background: #111; border: 1px solid #2a2a2a;
                         border-radius: 6px; color: #e0e0e0; padding: 7px 10px; font-size: 0.85rem; }
    input[type=number]:focus { outline: none; border-color: #6366f1; }

    .actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 16px; }
    .btn { padding: 9px; border-radius: 7px; border: none; font-size: 0.85rem;
           font-weight: 600; cursor: pointer; transition: opacity 0.15s; text-align: center; }
    .btn:hover:not(:disabled) { opacity: 0.8; }
    .btn:disabled { opacity: 0.3; cursor: default; }
    .btn-save  { background: #6366f1; color: #fff; grid-column: span 2; }
    .btn-start { background: #22c55e; color: #000; }
    .btn-stop  { background: #ef4444; color: #fff; }

    /* Trades */
    .trades-section { margin-top: 18px; }
    table { width: 100%; border-collapse: collapse; font-size: 0.78rem; }
    th { text-align: left; padding: 6px 8px; color: #555; font-weight: 500;
         border-bottom: 1px solid #222; white-space: nowrap; }
    td { padding: 7px 8px; border-bottom: 1px solid #1e1e1e; }
    tr:last-child td { border-bottom: none; }
    .side-up { color: #60a5fa; font-weight: 600; }
    .side-dn { color: #f472b6; font-weight: 600; }
    .pnl-pos { color: #22c55e; font-weight: 600; }
    .pnl-neg { color: #ef4444; font-weight: 600; }
    .reason  { font-size: 0.7rem; color: #555; }
  </style>
</head>
<body>
  <h1>Polypanic</h1>

  <div class="bots">
  {% for bot in bots %}
    <div class="bot {{ 'running' if bot.running }}">

      <!-- Header -->
      <div class="bot-header">
        <div class="dot {{ 'running' if bot.running else 'stopped' }}"></div>
        <span class="bot-name">{{ bot.name }}</span>
        <span class="bot-bankroll {{ bot.bankroll_class }}">{{ bot.bankroll_str }}</span>
      </div>

      <!-- Stats -->
      <div class="stats">
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

      <!-- Parameters form -->
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

      <!-- Recent trades -->
      <div class="trades-section">
        <h2 style="margin-top:18px;">Recent trades</h2>
        {% if bot.trades %}
        <table>
          <thead>
            <tr><th>Time</th><th>Side</th><th>Action</th><th>Price</th><th>P&L</th><th>Bankroll</th></tr>
          </thead>
          <tbody>
            {% for t in bot.trades %}
            <tr>
              <td style="color:#444">{{ t.time }}</td>
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
          <p style="color:#444; font-size:0.82rem;">No trades yet.</p>
        {% endif %}
      </div>

    </div>
  {% endfor %}
  </div>

  <script>setTimeout(() => location.reload(), 5000);</script>
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


def get_db_data(bot):
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
               ROUND(price, 3)        AS price,
               ROUND(pnl, 2)          AS pnl,
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
    cfg                     = load_config(bot)
    pid                     = get_pid(bot)
    trades, stats, bankroll = get_db_data(bot)

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
            # Wait up to 5 seconds for the process to exit cleanly
            for _ in range(10):
                time.sleep(0.5)
                try:
                    os.kill(pid, 0)  # still alive?
                except ProcessLookupError:
                    break  # gone
            else:
                # Still alive after 5s — force kill
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
