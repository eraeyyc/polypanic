#!/usr/bin/env python3
"""
Polypanic Dashboard — local web UI for controlling the paper trader.
Run with: ./run.sh dashboard.py
Then open: http://localhost:5000
"""

import json
import os
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template_string, request

app = Flask(__name__)

CONFIG_FILE = Path(__file__).parent / "config.json"
DB_FILE     = Path(__file__).parent / "polymarket_observer.db"
PID_FILE    = Path(__file__).parent / ".observer.pid"

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
    h2 { font-size: 0.85rem; font-weight: 600; text-transform: uppercase;
         letter-spacing: 0.08em; color: #888; margin-bottom: 16px; }

    .grid { display: grid; grid-template-columns: 340px 1fr; gap: 24px; }

    .card { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 10px; padding: 24px; }

    /* Status bar */
    .status { display: flex; align-items: center; gap: 10px; margin-bottom: 24px;
              background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 10px;
              padding: 16px 24px; }
    .dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
    .dot.running { background: #22c55e; box-shadow: 0 0 8px #22c55e88; }
    .dot.stopped { background: #666; }
    .status-label { font-size: 0.95rem; color: #aaa; }
    .status-label strong { color: #fff; }
    .bankroll { margin-left: auto; font-size: 1.1rem; font-weight: 600; }
    .bankroll.up   { color: #22c55e; }
    .bankroll.down { color: #ef4444; }
    .bankroll.flat { color: #aaa; }

    /* Form */
    .field { margin-bottom: 18px; }
    label { display: block; font-size: 0.8rem; color: #888; margin-bottom: 6px; }
    label span { float: right; color: #ccc; font-weight: 600; }
    input[type=range] { width: 100%; accent-color: #6366f1; cursor: pointer; }
    input[type=number] { width: 100%; background: #111; border: 1px solid #333;
                         border-radius: 6px; color: #e0e0e0; padding: 8px 10px;
                         font-size: 0.9rem; }
    input[type=number]:focus { outline: none; border-color: #6366f1; }

    .btn { display: inline-block; padding: 10px 20px; border-radius: 7px; border: none;
           font-size: 0.9rem; font-weight: 600; cursor: pointer; transition: opacity 0.15s; }
    .btn:hover { opacity: 0.85; }
    .btn-start { background: #22c55e; color: #000; width: 100%; margin-top: 8px; }
    .btn-stop  { background: #ef4444; color: #fff; width: 100%; margin-top: 8px; }
    .btn-save  { background: #6366f1; color: #fff; width: 100%; margin-top: 4px; }

    /* Trades table */
    table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
    th { text-align: left; padding: 8px 12px; color: #666; font-weight: 500;
         border-bottom: 1px solid #2a2a2a; white-space: nowrap; }
    td { padding: 9px 12px; border-bottom: 1px solid #1e1e1e; }
    tr:last-child td { border-bottom: none; }
    .side-up   { color: #60a5fa; font-weight: 600; }
    .side-dn   { color: #f472b6; font-weight: 600; }
    .pnl-pos   { color: #22c55e; font-weight: 600; }
    .pnl-neg   { color: #ef4444; font-weight: 600; }
    .reason    { font-size: 0.75rem; color: #888; }

    /* Summary row */
    .summary { display: flex; gap: 24px; margin-bottom: 20px; flex-wrap: wrap; }
    .stat { background: #111; border: 1px solid #222; border-radius: 8px;
            padding: 12px 18px; flex: 1; min-width: 100px; }
    .stat-label { font-size: 0.72rem; color: #666; text-transform: uppercase;
                  letter-spacing: 0.06em; margin-bottom: 4px; }
    .stat-value { font-size: 1.15rem; font-weight: 700; color: #fff; }
    .stat-value.up   { color: #22c55e; }
    .stat-value.down { color: #ef4444; }
  </style>
</head>
<body>
  <h1>Polypanic</h1>

  <!-- Status bar -->
  <div class="status">
    <div class="dot {{ 'running' if running else 'stopped' }}"></div>
    <span class="status-label">
      <strong>{{ 'Running' if running else 'Stopped' }}</strong>
      {% if running %} — PID {{ pid }}{% endif %}
    </span>
    <span class="bankroll {{ bankroll_class }}">{{ bankroll }}</span>
  </div>

  <div class="grid">
    <!-- Left: controls -->
    <div>
      <div class="card">
        <h2>Parameters</h2>
        <form method="POST" action="/save">
          <div class="field">
            <label>Entry threshold <span id="ev">≤ ${{ cfg.entry_threshold }}</span></label>
            <input type="range" name="entry_threshold" min="0.20" max="0.50" step="0.01"
                   value="{{ cfg.entry_threshold }}"
                   oninput="document.getElementById('ev').textContent='≤ $'+parseFloat(this.value).toFixed(2)">
          </div>
          <div class="field">
            <label>Exit threshold <span id="xv">≥ ${{ cfg.exit_threshold }}</span></label>
            <input type="range" name="exit_threshold" min="0.50" max="0.90" step="0.01"
                   value="{{ cfg.exit_threshold }}"
                   oninput="document.getElementById('xv').textContent='≥ $'+parseFloat(this.value).toFixed(2)">
          </div>
          <div class="field">
            <label>Stop-loss price <span id="slv">${{ cfg.stop_loss }}</span></label>
            <input type="range" name="stop_loss" min="0.00" max="0.30" step="0.01"
                   value="{{ cfg.stop_loss }}"
                   oninput="document.getElementById('slv').textContent='$'+parseFloat(this.value).toFixed(2)">
          </div>
          <div class="field">
            <label>Stop-loss after <span id="sav">{{ cfg.stop_loss_after }}s remaining</span></label>
            <input type="range" name="stop_loss_after" min="0" max="180" step="5"
                   value="{{ cfg.stop_loss_after }}"
                   oninput="document.getElementById('sav').textContent=this.value+'s remaining'">
          </div>
          <div class="field">
            <label>Entry delay <span id="edv">{{ cfg.entry_delay }}s</span></label>
            <input type="range" name="entry_delay" min="0" max="30" step="1"
                   value="{{ cfg.entry_delay }}"
                   oninput="document.getElementById('edv').textContent=this.value+'s'">
          </div>
          <div class="field">
            <label>BTC momentum filter <span id="bmv">${{ cfg.btc_momentum }}</span></label>
            <input type="range" name="btc_momentum" min="0" max="100" step="5"
                   value="{{ cfg.btc_momentum }}"
                   oninput="document.getElementById('bmv').textContent='$'+this.value">
          </div>
          <div class="field">
            <label>Starting bankroll</label>
            <input type="number" name="bankroll" value="{{ cfg.bankroll }}" step="50" min="100">
          </div>
          <button type="submit" class="btn btn-save">Save parameters</button>
        </form>

        <form method="POST" action="/start" style="margin-top:16px;">
          <button type="submit" class="btn btn-start" {{ 'disabled' if running }}>
            ▶ Start bot
          </button>
        </form>
        <form method="POST" action="/stop">
          <button type="submit" class="btn btn-stop" {{ 'disabled' if not running }}>
            ■ Stop bot
          </button>
        </form>
      </div>
    </div>

    <!-- Right: stats + trades -->
    <div>
      <div class="card" style="margin-bottom:24px;">
        <h2>Session stats</h2>
        <div class="summary">
          <div class="stat">
            <div class="stat-label">Total P&L</div>
            <div class="stat-value {{ 'up' if stats.total_pnl >= 0 else 'down' }}">
              {{ '%+.2f'|format(stats.total_pnl) }}
            </div>
          </div>
          <div class="stat">
            <div class="stat-label">Win rate</div>
            <div class="stat-value">{{ stats.win_rate }}%</div>
          </div>
          <div class="stat">
            <div class="stat-label">Trades</div>
            <div class="stat-value">{{ stats.total_sells }}</div>
          </div>
          <div class="stat">
            <div class="stat-label">exit_target P&L</div>
            <div class="stat-value up">{{ '%+.2f'|format(stats.exit_target_pnl) }}</div>
          </div>
          <div class="stat">
            <div class="stat-label">force_exit P&L</div>
            <div class="stat-value down">{{ '%+.2f'|format(stats.force_exit_pnl) }}</div>
          </div>
        </div>
      </div>

      <div class="card">
        <h2>Recent trades</h2>
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Side</th>
              <th>Action</th>
              <th>Price</th>
              <th>P&L</th>
              <th>Bankroll</th>
            </tr>
          </thead>
          <tbody>
            {% for t in trades %}
            <tr>
              <td style="color:#555">{{ t.time }}</td>
              <td class="{{ 'side-up' if t.side == 'up' else 'side-dn' }}">{{ t.side.upper() }}</td>
              <td>
                {{ t.action }}
                <div class="reason">{{ t.reason }}</div>
              </td>
              <td>${{ '%.3f'|format(t.price) }}</td>
              <td class="{{ 'pnl-pos' if t.pnl > 0 else ('pnl-neg' if t.pnl < 0 else '') }}">
                {% if t.action == 'sell' %}{{ '%+.2f'|format(t.pnl) }}{% endif %}
              </td>
              <td>${{ '%.2f'|format(t.bankroll_after) }}</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <script>
    // Auto-refresh every 5 seconds
    setTimeout(() => location.reload(), 5000);
  </script>
</body>
</html>
"""


def load_config():
    if CONFIG_FILE.exists():
        return {**DEFAULTS, **json.loads(CONFIG_FILE.read_text())}
    return dict(DEFAULTS)


def save_config(data):
    CONFIG_FILE.write_text(json.dumps(data, indent=2))


def get_pid():
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)  # check if alive
            return pid
        except (ProcessLookupError, ValueError, OSError):
            PID_FILE.unlink(missing_ok=True)
    return None


def get_db_data():
    if not DB_FILE.exists():
        return [], {
            "total_pnl": 0, "win_rate": 0, "total_sells": 0,
            "exit_target_pnl": 0, "force_exit_pnl": 0,
        }, None

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row

    trades = conn.execute("""
        SELECT side, action, reason,
               ROUND(price, 3) as price,
               ROUND(pnl, 2) as pnl,
               ROUND(bankroll_after, 2) as bankroll_after,
               strftime('%H:%M:%S', datetime(timestamp, 'unixepoch')) as time
        FROM paper_trades ORDER BY id DESC LIMIT 30
    """).fetchall()

    row = conn.execute("""
        SELECT
            SUM(CASE WHEN action='sell' THEN pnl ELSE 0 END)                        AS total_pnl,
            SUM(CASE WHEN action='sell' AND pnl > 0 THEN 1 ELSE 0 END) * 100.0 /
                MAX(SUM(CASE WHEN action='sell' THEN 1 ELSE 0 END), 1)              AS win_rate,
            SUM(CASE WHEN action='sell' THEN 1 ELSE 0 END)                          AS total_sells,
            SUM(CASE WHEN reason='exit_target' THEN pnl ELSE 0 END)                 AS exit_target_pnl,
            SUM(CASE WHEN reason='force_exit'  THEN pnl ELSE 0 END)                 AS force_exit_pnl
        FROM paper_trades
    """).fetchone()

    last = conn.execute(
        "SELECT bankroll_after FROM paper_trades ORDER BY id DESC LIMIT 1"
    ).fetchone()

    conn.close()

    stats = {
        "total_pnl":      round(row["total_pnl"] or 0, 2),
        "win_rate":       round(row["win_rate"] or 0, 1),
        "total_sells":    row["total_sells"] or 0,
        "exit_target_pnl": round(row["exit_target_pnl"] or 0, 2),
        "force_exit_pnl":  round(row["force_exit_pnl"] or 0, 2),
    }
    bankroll = round(last["bankroll_after"], 2) if last else None
    return [dict(t) for t in trades], stats, bankroll


@app.route("/")
def index():
    cfg                   = load_config()
    pid                   = get_pid()
    trades, stats, bankroll = get_db_data()

    if bankroll is None:
        bankroll_str   = "$—"
        bankroll_class = "flat"
    else:
        diff           = bankroll - cfg["bankroll"]
        bankroll_str   = f"${bankroll:,.2f}"
        bankroll_class = "up" if diff > 0 else ("down" if diff < 0 else "flat")

    return render_template_string(
        HTML,
        cfg=type("cfg", (), cfg)(),
        running=pid is not None,
        pid=pid,
        bankroll=bankroll_str,
        bankroll_class=bankroll_class,
        trades=trades,
        stats=stats,
    )


@app.route("/save", methods=["POST"])
def save():
    cfg = {
        "entry_threshold": float(request.form["entry_threshold"]),
        "exit_threshold":  float(request.form["exit_threshold"]),
        "stop_loss":       float(request.form["stop_loss"]),
        "stop_loss_after": int(request.form["stop_loss_after"]),
        "entry_delay":     int(request.form["entry_delay"]),
        "btc_momentum":    float(request.form["btc_momentum"]),
        "bankroll":        float(request.form["bankroll"]),
        "poll":            load_config().get("poll", 3.0),
    }
    save_config(cfg)
    return redirect("/")


@app.route("/start", methods=["POST"])
def start():
    if get_pid():
        return redirect("/")

    cfg = load_config()
    cmd = [
        sys.executable, "observer.py",
        "--entry",          str(cfg["entry_threshold"]),
        "--exit",           str(cfg["exit_threshold"]),
        "--stop-loss",      str(cfg["stop_loss"]),
        "--stop-loss-after", str(cfg["stop_loss_after"]),
        "--entry-delay",    str(cfg["entry_delay"]),
        "--btc-momentum",   str(cfg["btc_momentum"]),
        "--bankroll",       str(cfg["bankroll"]),
        "--poll",           str(cfg["poll"]),
    ]
    proc = subprocess.Popen(cmd, cwd=Path(__file__).parent)
    PID_FILE.write_text(str(proc.pid))
    return redirect("/")


@app.route("/stop", methods=["POST"])
def stop():
    pid = get_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        PID_FILE.unlink(missing_ok=True)
    return redirect("/")


if __name__ == "__main__":
    print("Dashboard running at http://localhost:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)
