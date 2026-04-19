# -*- coding: utf-8 -*-
"""
Metricas del backtest HYPE Support Zone Strategy
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import pandas as pd
import numpy as np
from pathlib import Path

# reutiliza el motor del backtest
sys.path.insert(0, str(Path(__file__).parent.parent))
from backtest.zone_strategy import (
    run_backtest, compute_pnl_series, ZONES_CFG, DATA_FILE
)

# ── CARGA Y EJECUCION ──────────────────────────────────────────────────────────
df = pd.read_csv(DATA_FILE, index_col="datetime", parse_dates=True)
positions, df_rsi = run_backtest(df)

last_price  = df_rsi["close"].iloc[-1]
total_cap   = sum(cfg["capital"] for cfg in ZONES_CFG)   # $9,000

period_start = df_rsi.index[0]
period_end   = df_rsi.index[-1]
period_days  = (period_end - period_start).total_seconds() / 86400
period_weeks = period_days / 7
period_months = period_days / 30.44

# ── 1. DURACION DE CADA OPERACION ─────────────────────────────────────────────
durations_h = []   # horas que estuvo abierta cada posicion

for p in positions:
    if p.reentry:
        close_time = p.exit1_time if p.tp1_hit else period_end
    else:
        if p.tp2_hit:
            close_time = p.exit2_time
        elif p.tp1_hit:
            close_time = period_end   # TP2 aun no alcanzado
        else:
            close_time = period_end
    hours = (close_time - p.entry_time).total_seconds() / 3600
    durations_h.append(hours)

# ── 2. CAPITAL LIBRE EN CADA VELA ─────────────────────────────────────────────
# Construye serie de capital-en-uso hora a hora
capital_in_use = pd.Series(0.0, index=df_rsi.index)

for p in positions:
    # tiempo de cierre real del capital
    if p.reentry:
        close_time = p.exit1_time if p.tp1_hit else period_end
        in_use_mask = (df_rsi.index >= p.entry_time) & (df_rsi.index < close_time)
        capital_in_use[in_use_mask] += p.capital
    else:
        # TP1 libera 50% del capital, TP2 libera el resto
        if p.tp1_hit and p.tp2_hit:
            # 100% en uso de entry a TP1; 50% de TP1 a TP2
            mask_full = (df_rsi.index >= p.entry_time) & (df_rsi.index < p.exit1_time)
            mask_half = (df_rsi.index >= p.exit1_time) & (df_rsi.index < p.exit2_time)
            capital_in_use[mask_full] += p.capital
            capital_in_use[mask_half] += p.capital / 2
        elif p.tp1_hit:
            mask_full = (df_rsi.index >= p.entry_time) & (df_rsi.index < p.exit1_time)
            mask_half = df_rsi.index >= p.exit1_time
            capital_in_use[mask_full] += p.capital
            capital_in_use[mask_half] += p.capital / 2
        else:
            mask_full = df_rsi.index >= p.entry_time
            capital_in_use[mask_full] += p.capital

capital_free = (total_cap - capital_in_use).clip(lower=0)

# ── 3. PNL POR OPERACION ──────────────────────────────────────────────────────
pnl_per_pos = []
for p in positions:
    if p.closed:
        pnl_per_pos.append(p.net_pnl)
    elif not p.reentry and p.tp1_hit:
        tp1_fee    = (p.qty / 2) * p.tp1 * 0.0001
        realized   = (p.qty / 2) * (p.tp1 - p.entry_price) - p.entry_fee - tp1_fee
        unrealized = (p.qty / 2) * (last_price - p.entry_price)
        pnl_per_pos.append(realized + unrealized)
    else:
        pnl_per_pos.append(p.qty * (last_price - p.entry_price) - p.entry_fee)

total_net    = sum(pnl_per_pos)
total_return = total_net / total_cap
ann_return   = (1 + total_return) ** (365 / period_days) - 1

n_ops        = len(positions)
ops_por_dia  = n_ops / period_days
ops_por_sem  = n_ops / period_weeks
ops_por_mes  = n_ops / period_months

avg_pnl      = np.mean(pnl_per_pos)
avg_dur_h    = np.mean(durations_h)
avg_dur_d    = avg_dur_h / 24
avg_free_usd = capital_free.mean()
avg_free_pct = avg_free_usd / total_cap * 100

# ── 4. DISTRIBUCION DE DURACION ───────────────────────────────────────────────
dur_series   = pd.Series(durations_h)
pct_lt_6h    = (dur_series < 6).mean() * 100
pct_lt_24h   = (dur_series < 24).mean() * 100

# ── 5. PNL POR ZONA ───────────────────────────────────────────────────────────
zone_stats = {}
for z in [1, 2, 3]:
    zp = [p for p in positions if p.zone == z]
    zpnl = [pnl_per_pos[i] for i, p in enumerate(positions) if p.zone == z]
    zone_stats[z] = {
        "ops":  len(zp),
        "pnl":  sum(zpnl),
        "cap":  next(c["capital"] for c in ZONES_CFG if c["level"] == z),
    }

# ── REPORTE ────────────────────────────────────────────────────────────────────
SEP = "=" * 65

print(f"\n{SEP}")
print("  METRICAS DEL BACKTEST  --  HYPE Support Zone Strategy")
print(f"  Periodo: {period_start.date()} al {period_end.date()} ({period_days:.0f} dias)")
print(SEP)

print("\n[FRECUENCIA DE OPERACIONES]")
print(f"  Total operaciones        : {n_ops}")
print(f"  Promedio por MES         : {ops_por_mes:.1f} ops/mes")
print(f"  Promedio por SEMANA      : {ops_por_sem:.1f} ops/sem")
print(f"  Promedio por DIA         : {ops_por_dia:.2f} ops/dia")

print("\n[RENTABILIDAD]")
print(f"  Ganancia promedio/op.    : ${avg_pnl:+.2f}")
print(f"  Ganancia total neta      : ${total_net:+,.2f}")
print(f"  Retorno sobre capital    : {total_return*100:+.2f}%")
print(f"  Retorno ANUALIZADO       : {ann_return*100:+.1f}%  (proyeccion)")
print(f"  Total comisiones         : ${sum(p.fees for p in positions):.2f}")

print("\n[DURACION DE OPERACIONES]")
print(f"  Duracion promedio        : {avg_dur_h:.1f} horas  ({avg_dur_d:.1f} dias)")
print(f"  Duracion minima          : {min(durations_h):.1f} horas")
print(f"  Duracion maxima          : {max(durations_h):.1f} horas  ({max(durations_h)/24:.1f} dias)")
print(f"  Ops cerradas en < 6 h    : {pct_lt_6h:.0f}%")
print(f"  Ops cerradas en < 24 h   : {pct_lt_24h:.0f}%")

print("\n[CAPITAL LIBRE (sobre $9,000 total)]")
print(f"  Capital libre promedio   : ${avg_free_usd:,.0f}  ({avg_free_pct:.1f}%)")
print(f"  Capital libre MAXIMO     : ${capital_free.max():,.0f}  ({capital_free.max()/total_cap*100:.1f}%)")
print(f"  Capital libre MINIMO     : ${capital_free.min():,.0f}  ({capital_free.min()/total_cap*100:.1f}%)")
pct_fully_free = (capital_in_use == 0).mean() * 100
pct_fully_used = (capital_free == 0).mean() * 100
print(f"  Tiempo sin ops. abiertas : {pct_fully_free:.1f}% del periodo")
print(f"  Tiempo capital 100% usado: {pct_fully_used:.1f}% del periodo")

print("\n[PNL POR ZONA]")
for z, st in zone_stats.items():
    roi = st["pnl"] / st["cap"] * 100
    print(f"  Zona {z} (cap ${st['cap']:,.0f})  : {st['ops']} ops | "
          f"PnL ${st['pnl']:+,.2f} | ROI {roi:+.1f}%")

print(f"\n{SEP}\n")
