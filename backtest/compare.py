# -*- coding: utf-8 -*-
"""
Comparativa backtest: 3 zonas vs 4 zonas (+ Z4 $45.50-$46.20 / $1,500 / RSI40)
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from pathlib import Path
from copy import deepcopy

sys.path.insert(0, str(Path(__file__).parent.parent))
from backtest.zone_strategy import (
    run_backtest, compute_pnl_series, DATA_FILE,
    TAKER_FEE, MAKER_FEE, TP1_PCT, TP2_PCT, REENTRY_TP_PCT,
)

# ── CONFIGURACIONES ────────────────────────────────────────────────────────────
ZONES_3 = [
    dict(level=1, low=42.98, high=43.50, rsi_entry=40, capital=2000.0),
    dict(level=2, low=40.22, high=40.60, rsi_entry=40, capital=3000.0),
    dict(level=3, low=37.80, high=38.35, rsi_entry=30, capital=4000.0),
]
ZONES_4 = ZONES_3 + [
    dict(level=4, low=45.50, high=46.20, rsi_entry=40, capital=1500.0),
]

# ── HELPERS ────────────────────────────────────────────────────────────────────
def pnl_of(pos, last_price):
    if pos.closed:
        return pos.net_pnl
    if not pos.reentry and pos.tp1_hit:
        tp1_fee    = (pos.qty / 2) * pos.tp1 * MAKER_FEE
        realized   = (pos.qty / 2) * (pos.tp1 - pos.entry_price) - pos.entry_fee - tp1_fee
        unrealized = (pos.qty / 2) * (last_price - pos.entry_price)
        return realized + unrealized
    return pos.qty * (last_price - pos.entry_price) - pos.entry_fee


def duration_h(pos, period_end):
    if pos.reentry:
        t = pos.exit1_time if pos.tp1_hit else period_end
    else:
        if pos.tp2_hit:   t = pos.exit2_time
        elif pos.tp1_hit: t = period_end
        else:             t = period_end
    return (t - pos.entry_time).total_seconds() / 3600


def free_capital_series(df_idx, positions, zones_cfg):
    total_cap = sum(c["capital"] for c in zones_cfg)
    in_use    = pd.Series(0.0, index=df_idx)
    period_end = df_idx[-1]

    for p in positions:
        if p.reentry:
            close_t = p.exit1_time if p.tp1_hit else period_end
            mask    = (df_idx >= p.entry_time) & (df_idx < close_t)
            in_use[mask] += p.capital
        else:
            if p.tp1_hit and p.tp2_hit:
                m1 = (df_idx >= p.entry_time)  & (df_idx < p.exit1_time)
                m2 = (df_idx >= p.exit1_time)   & (df_idx < p.exit2_time)
                in_use[m1] += p.capital
                in_use[m2] += p.capital / 2
            elif p.tp1_hit:
                m1 = (df_idx >= p.entry_time)  & (df_idx < p.exit1_time)
                m2 = df_idx >= p.exit1_time
                in_use[m1] += p.capital
                in_use[m2] += p.capital / 2
            else:
                in_use[df_idx >= p.entry_time] += p.capital

    return (total_cap - in_use).clip(lower=0), total_cap


def compute_metrics(positions, df_rsi, zones_cfg):
    last_price  = df_rsi["close"].iloc[-1]
    period_end  = df_rsi.index[-1]
    period_start = df_rsi.index[0]
    period_days  = (period_end - period_start).total_seconds() / 86400
    total_cap    = sum(c["capital"] for c in zones_cfg)

    pnls       = [pnl_of(p, last_price) for p in positions]
    durs       = [duration_h(p, period_end) for p in positions]
    free_s, _  = free_capital_series(df_rsi.index, positions, zones_cfg)

    total_net    = sum(pnls)
    total_return = total_net / total_cap
    ann_return   = (1 + total_return) ** (365 / period_days) - 1

    closed  = [p for p in positions if p.closed]
    winners = [p for p in closed if p.net_pnl > 0]

    zone_pnl = {}
    for cfg in zones_cfg:
        z = cfg["level"]
        zp = [(pnl_of(p, last_price)) for p in positions if p.zone == z]
        zone_pnl[z] = {"ops": len(zp), "pnl": sum(zp), "cap": cfg["capital"]}

    return {
        "n_ops":          len(positions),
        "n_inicial":      sum(1 for p in positions if not p.reentry),
        "n_reentry":      sum(1 for p in positions if p.reentry),
        "n_closed":       len(closed),
        "win_rate":       len(winners) / len(closed) * 100 if closed else 0,
        "total_net":      total_net,
        "total_cap":      total_cap,
        "total_return":   total_return * 100,
        "ann_return":     ann_return * 100,
        "avg_pnl":        np.mean(pnls) if pnls else 0,
        "total_fees":     sum(p.fees for p in positions),
        "avg_dur_h":      np.mean(durs) if durs else 0,
        "min_dur_h":      min(durs) if durs else 0,
        "max_dur_h":      max(durs) if durs else 0,
        "pct_lt_6h":      np.mean([d < 6  for d in durs]) * 100 if durs else 0,
        "pct_lt_24h":     np.mean([d < 24 for d in durs]) * 100 if durs else 0,
        "avg_free_usd":   free_s.mean(),
        "avg_free_pct":   free_s.mean() / total_cap * 100,
        "pct_no_ops":     (free_s == total_cap).mean() * 100,
        "ops_mes":        len(positions) / (period_days / 30.44),
        "ops_sem":        len(positions) / (period_days / 7),
        "ops_dia":        len(positions) / period_days,
        "period_days":    period_days,
        "zone_pnl":       zone_pnl,
        "free_series":    free_s,
        "pnl_series":     compute_pnl_series(df_rsi, positions),
        "positions":      positions,
    }


# ── EJECUCION ──────────────────────────────────────────────────────────────────
df = pd.read_csv(DATA_FILE, index_col="datetime", parse_dates=True)
print(f"Datos: {len(df)} velas | {df.index[0].date()} al {df.index[-1].date()}\n")

import importlib, backtest.zone_strategy as zmod

# Backtest 3 zonas
zmod.ZONES_CFG = ZONES_3
pos3, df3 = run_backtest(df)
m3 = compute_metrics(pos3, df3, ZONES_3)

# Backtest 4 zonas
zmod.ZONES_CFG = ZONES_4
pos4, df4 = run_backtest(df)
m4 = compute_metrics(pos4, df4, ZONES_4)

# ── REPORTE COMPARATIVO ────────────────────────────────────────────────────────
def fmt_diff(v4, v3, pct=False, invert=False):
    diff = v4 - v3
    sym  = "+" if diff >= 0 else ""
    tag  = "%" if pct else ""
    arrow = " (=)" if diff == 0 else (" [^]" if (diff > 0) != invert else " [v]")
    return f"{sym}{diff:.1f}{tag}{arrow}"

SEP  = "=" * 72
SEP2 = "-" * 72

print(f"\n{SEP}")
print("  COMPARATIVA BACKTEST  --  3 Zonas vs 4 Zonas")
print(f"  Nueva zona: Z4  $45.50-$46.20  |  Capital $1,500  |  RSI 40")
print(SEP)

print(f"\n{'Metrica':<32} {'3 Zonas':>12} {'4 Zonas':>12} {'Diferencia':>14}")
print(SEP2)

rows = [
    ("Capital total",           f"${m3['total_cap']:,.0f}",   f"${m4['total_cap']:,.0f}",   f"+${m4['total_cap']-m3['total_cap']:,.0f}"),
    ("Operaciones totales",     str(m3["n_ops"]),              str(m4["n_ops"]),              fmt_diff(m4["n_ops"],      m3["n_ops"])),
    ("  Iniciales",             str(m3["n_inicial"]),          str(m4["n_inicial"]),          fmt_diff(m4["n_inicial"],  m3["n_inicial"])),
    ("  Re-entradas",           str(m3["n_reentry"]),          str(m4["n_reentry"]),          fmt_diff(m4["n_reentry"],  m3["n_reentry"])),
    ("Cerradas",                str(m3["n_closed"]),           str(m4["n_closed"]),           fmt_diff(m4["n_closed"],   m3["n_closed"])),
    ("Win rate",                f"{m3['win_rate']:.1f}%",      f"{m4['win_rate']:.1f}%",      fmt_diff(m4["win_rate"],   m3["win_rate"], pct=True)),
    (SEP2, "", "", ""),
    ("PnL neto total",          f"${m3['total_net']:+,.2f}",   f"${m4['total_net']:+,.2f}",   f"${m4['total_net']-m3['total_net']:+,.2f}"),
    ("Retorno s/ capital",      f"{m3['total_return']:+.2f}%", f"{m4['total_return']:+.2f}%", fmt_diff(m4["total_return"], m3["total_return"], pct=True)),
    ("Retorno ANUALIZADO",      f"{m3['ann_return']:+.1f}%",   f"{m4['ann_return']:+.1f}%",   fmt_diff(m4["ann_return"],   m3["ann_return"], pct=True)),
    ("Ganancia prom./op.",      f"${m3['avg_pnl']:+.2f}",      f"${m4['avg_pnl']:+.2f}",      f"${m4['avg_pnl']-m3['avg_pnl']:+.2f}"),
    ("Total comisiones",        f"${m3['total_fees']:.2f}",    f"${m4['total_fees']:.2f}",    f"${m4['total_fees']-m3['total_fees']:+.2f}"),
    (SEP2, "", "", ""),
    ("Ops por mes",             f"{m3['ops_mes']:.1f}",        f"{m4['ops_mes']:.1f}",        fmt_diff(m4["ops_mes"],    m3["ops_mes"])),
    ("Ops por semana",          f"{m3['ops_sem']:.1f}",        f"{m4['ops_sem']:.1f}",        fmt_diff(m4["ops_sem"],    m3["ops_sem"])),
    ("Ops por dia",             f"{m3['ops_dia']:.2f}",        f"{m4['ops_dia']:.2f}",        fmt_diff(m4["ops_dia"],    m3["ops_dia"])),
    (SEP2, "", "", ""),
    ("Dur. promedio (h)",       f"{m3['avg_dur_h']:.1f}h",     f"{m4['avg_dur_h']:.1f}h",     fmt_diff(m4["avg_dur_h"],  m3["avg_dur_h"])),
    ("Dur. minima (h)",         f"{m3['min_dur_h']:.1f}h",     f"{m4['min_dur_h']:.1f}h",     fmt_diff(m4["min_dur_h"],  m3["min_dur_h"])),
    ("Dur. maxima (dias)",      f"{m3['max_dur_h']/24:.1f}d",  f"{m4['max_dur_h']/24:.1f}d",  fmt_diff(m4["max_dur_h"]/24, m3["max_dur_h"]/24)),
    ("Ops cerradas < 6h",       f"{m3['pct_lt_6h']:.0f}%",     f"{m4['pct_lt_6h']:.0f}%",     fmt_diff(m4["pct_lt_6h"],  m3["pct_lt_6h"], pct=True)),
    ("Ops cerradas < 24h",      f"{m3['pct_lt_24h']:.0f}%",    f"{m4['pct_lt_24h']:.0f}%",    fmt_diff(m4["pct_lt_24h"], m3["pct_lt_24h"], pct=True)),
    (SEP2, "", "", ""),
    ("Capital libre prom.",     f"${m3['avg_free_usd']:,.0f}", f"${m4['avg_free_usd']:,.0f}", f"${m4['avg_free_usd']-m3['avg_free_usd']:+,.0f}"),
    ("Capital libre prom. %",   f"{m3['avg_free_pct']:.1f}%",  f"{m4['avg_free_pct']:.1f}%",  fmt_diff(m4["avg_free_pct"], m3["avg_free_pct"], pct=True, invert=True)),
    ("Tiempo sin posiciones",   f"{m3['pct_no_ops']:.1f}%",    f"{m4['pct_no_ops']:.1f}%",    fmt_diff(m4["pct_no_ops"],   m3["pct_no_ops"], pct=True, invert=True)),
]

for r in rows:
    if r[0] == SEP2:
        print(SEP2)
    else:
        print(f"  {r[0]:<30} {r[1]:>12} {r[2]:>12} {r[3]:>14}")

# Detalle por zona (4 zonas)
print(f"\n-- DETALLE POR ZONA (4 zonas) {'-'*40}")
print(f"  {'Zona':<22} {'Capital':>8} {'Ops':>5} {'PnL neto':>10} {'ROI':>7}")
print(f"  {'-'*55}")
for cfg in ZONES_4:
    z   = cfg["level"]
    st  = m4["zone_pnl"][z]
    roi = st["pnl"] / st["cap"] * 100
    tag = "  <<< NUEVA" if z == 4 else ""
    print(f"  Z{z} ${cfg['low']}-${cfg['high']:<8}  ${st['cap']:>6,.0f}  {st['ops']:>5}  "
          f"${st['pnl']:>+8,.2f}  {roi:>+6.1f}%{tag}")

total_pnl_4 = sum(st["pnl"] for st in m4["zone_pnl"].values())
print(f"  {'TOTAL':<22} ${m4['total_cap']:>6,.0f}  {m4['n_ops']:>5}  ${total_pnl_4:>+8,.2f}  "
      f"{m4['total_return']:>+6.1f}%")
print(f"\n{SEP}\n")

# ── GRAFICO COMPARATIVO ────────────────────────────────────────────────────────
fig, axes = plt.subplots(3, 1, figsize=(18, 13), sharex=False)

# Panel 1: PnL acumulado comparativo
ax = axes[0]
ax.plot(m3["pnl_series"].index, m3["pnl_series"].values,
        color="#1565C0", linewidth=1.2, label="3 Zonas  ($9,000)")
ax.plot(m4["pnl_series"].index, m4["pnl_series"].values,
        color="#E65100", linewidth=1.2, linestyle="--", label="4 Zonas  ($10,500)")
ax.axhline(0, color="gray", linewidth=0.6)
ax.fill_between(m4["pnl_series"].index,
                m4["pnl_series"].values, m3["pnl_series"].reindex(m4["pnl_series"].index, method="ffill").fillna(0).values,
                alpha=0.15, color="#E65100", label="Diferencia Z4")
ax.set_ylabel("PnL $ (neto)", fontsize=9)
ax.set_title("Comparativa PnL acumulado: 3 Zonas vs 4 Zonas", fontsize=11, fontweight="bold")
ax.legend(fontsize=9)
ax.grid(alpha=0.25)

# Panel 2: Precio + zonas
ax2 = axes[1]
ax2.plot(df4.index, df4["close"], color="#263238", linewidth=0.65, label="HYPE close")
colors = ["#2E7D32", "#E65100", "#B71C1C", "#7B1FA2"]
for cfg, col in zip(ZONES_4, colors):
    ax2.axhspan(cfg["low"], cfg["high"], alpha=0.18, color=col)
    lbl = f"Z{cfg['level']} ${cfg['low']}-${cfg['high']}"
    if cfg["level"] == 4:
        lbl += "  [NUEVA]"
    ax2.axhline((cfg["low"]+cfg["high"])/2, color=col, linewidth=0.5,
                linestyle=":", alpha=0.8, label=lbl)

# entradas zona 4
for p in pos4:
    if p.zone == 4:
        ax2.scatter(p.entry_time, p.entry_price, marker="^", color="#7B1FA2",
                    s=90, zorder=6, edgecolors="white", linewidths=0.5)
        if p.tp1_hit:
            ax2.scatter(p.exit1_time, p.tp1, marker="v", color="#CE93D8",
                        s=90, zorder=6, edgecolors="white", linewidths=0.5)
        if not p.reentry and p.tp2_hit:
            ax2.scatter(p.exit2_time, p.tp2, marker="v", color="#9C27B0",
                        s=90, zorder=6, edgecolors="white", linewidths=0.5)

ax2.set_ylabel("Precio (USDC)", fontsize=9)
ax2.set_title("Precio HYPE con 4 Zonas de Soporte/Resistencia", fontsize=11, fontweight="bold")
ax2.legend(loc="upper right", fontsize=7.5, ncol=2)
ax2.grid(alpha=0.25)

# Panel 3: Capital libre comparativo
ax3 = axes[2]
ax3.fill_between(m3["free_series"].index, m3["free_series"].values,
                 alpha=0.3, color="#1565C0", label=f"Capital libre 3Z (base $9,000)")
ax3.fill_between(m4["free_series"].index, m4["free_series"].values,
                 alpha=0.3, color="#E65100", label=f"Capital libre 4Z (base $10,500)")
ax3.plot(m3["free_series"].index, m3["free_series"].values,
         color="#1565C0", linewidth=0.8)
ax3.plot(m4["free_series"].index, m4["free_series"].values,
         color="#E65100", linewidth=0.8, linestyle="--")
ax3.axhline(m3["avg_free_usd"], color="#1565C0", linewidth=0.7, linestyle=":",
            alpha=0.8, label=f"Prom. 3Z ${m3['avg_free_usd']:,.0f}")
ax3.axhline(m4["avg_free_usd"], color="#E65100", linewidth=0.7, linestyle=":",
            alpha=0.8, label=f"Prom. 4Z ${m4['avg_free_usd']:,.0f}")
ax3.set_ylabel("Capital libre ($)", fontsize=9)
ax3.set_xlabel("Fecha", fontsize=9)
ax3.legend(fontsize=8, ncol=2)
ax3.grid(alpha=0.25)

plt.tight_layout(pad=2.0)
out = Path(__file__).parent.parent / "data" / "compare_chart.png"
plt.savefig(out, dpi=150, bbox_inches="tight")
print(f"Grafico guardado: {out}")
