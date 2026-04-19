# -*- coding: utf-8 -*-
"""
Backtest: HYPE Support Zone Strategy
3 zonas de soporte - RSI-14 - TP1 +2.5% / TP2 +4.5% - re-entradas +1.3%
Comisiones Hyperliquid: taker 0.05% (entradas) / maker 0.01% (TPs)
"""
import sys
import io
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")   # sin GUI — guarda PNG directamente
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from dataclasses import dataclass
from pathlib import Path

# fuerza UTF-8 en stdout para evitar errores de encoding en Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── CONFIGURACION ──────────────────────────────────────────────────────────────
DATA_FILE = Path(__file__).parent.parent / "data" / "hype_1h.csv"

ZONES_CFG = [
    dict(level=1, low=42.98, high=43.50, rsi_entry=40, capital=2000.0),
    dict(level=2, low=40.22, high=40.60, rsi_entry=40, capital=3000.0),
    dict(level=3, low=37.80, high=38.35, rsi_entry=30, capital=4000.0),
    dict(level=4, low=45.50, high=46.20, rsi_entry=40, capital=1500.0),
]

TP1_PCT        = 0.025   # +2.5%  — cierra 50% de la posicion
TP2_PCT        = 0.045   # +4.5%  — cierra el 50% restante
REENTRY_TP_PCT = 0.013   # +1.3%  — TP unico para re-entradas

TAKER_FEE = 0.0005       # 0.05%  entradas (market order)
MAKER_FEE = 0.0001       # 0.01%  salidas en TP (limit order)
RSI_PERIOD = 14


# ── RSI ────────────────────────────────────────────────────────────────────────
def compute_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    delta    = closes.diff()
    up       = delta.clip(lower=0)
    down     = -delta.clip(upper=0)
    avg_gain = up.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = down.ewm(com=period - 1, min_periods=period).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


# ── POSICION ───────────────────────────────────────────────────────────────────
@dataclass
class Position:
    zone:         int
    reentry:      bool
    entry_time:   object
    entry_price:  float
    qty:          float
    capital:      float
    tp1:          float
    tp2:          float
    entry_fee:    float = 0.0
    tp1_hit:      bool  = False
    tp2_hit:      bool  = False
    exit1_time:   object = None
    exit2_time:   object = None
    gross_pnl:    float  = 0.0
    fees:         float  = 0.0

    @property
    def closed(self) -> bool:
        return self.tp1_hit if self.reentry else self.tp2_hit

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fees

    @property
    def tag(self) -> str:
        return "RE-ENTRY" if self.reentry else "INICIAL"


# ── MOTOR DE BACKTEST ──────────────────────────────────────────────────────────
def run_backtest(df: pd.DataFrame) -> tuple:
    df = df.copy()
    df["rsi"] = compute_rsi(df["close"], RSI_PERIOD)

    zone_states  = {cfg["level"]: {"cfg": cfg, "positions": []} for cfg in ZONES_CFG}
    all_pos: list = []

    for i in range(1, len(df)):
        row       = df.iloc[i]
        ts        = df.index[i]
        prev_rsi  = df["rsi"].iat[i - 1]
        curr_rsi  = df["rsi"].iat[i]
        c_low     = row["low"]
        c_high    = row["high"]
        c_close   = row["close"]

        for lvl, state in zone_states.items():
            cfg           = state["cfg"]
            z_low, z_high = cfg["low"], cfg["high"]
            threshold     = cfg["rsi_entry"]
            capital       = cfg["capital"]

            # 1. Verificar TPs sobre posiciones abiertas
            open_pos = [p for p in state["positions"] if not p.closed]
            for pos in open_pos:
                if pos.reentry:
                    if not pos.tp1_hit and c_high >= pos.tp1:
                        pos.tp1_hit    = True
                        pos.exit1_time = ts
                        exit_fee       = pos.qty * pos.tp1 * MAKER_FEE
                        pos.fees      += exit_fee
                        pos.gross_pnl  = pos.qty * (pos.tp1 - pos.entry_price)
                else:
                    if not pos.tp1_hit and c_high >= pos.tp1:
                        pos.tp1_hit    = True
                        pos.exit1_time = ts
                        exit_fee       = (pos.qty / 2) * pos.tp1 * MAKER_FEE
                        pos.fees      += exit_fee
                        pos.gross_pnl += (pos.qty / 2) * (pos.tp1 - pos.entry_price)
                    if pos.tp1_hit and not pos.tp2_hit and c_high >= pos.tp2:
                        pos.tp2_hit    = True
                        pos.exit2_time = ts
                        exit_fee       = (pos.qty / 2) * pos.tp2 * MAKER_FEE
                        pos.fees      += exit_fee
                        pos.gross_pnl += (pos.qty / 2) * (pos.tp2 - pos.entry_price)

            # 2. Verificar condicion de nueva entrada
            rsi_valid = not (np.isnan(prev_rsi) or np.isnan(curr_rsi))
            rsi_cross = rsi_valid and (prev_rsi < threshold <= curr_rsi)
            in_zone   = (c_low <= z_high) and (c_close >= z_low * 0.98)

            if rsi_cross and in_zone:
                still_open  = [p for p in state["positions"] if not p.closed]
                is_reentry  = len(still_open) > 0
                entry_price = c_close
                qty         = capital / entry_price
                entry_fee   = qty * entry_price * TAKER_FEE

                if is_reentry:
                    pos = Position(
                        zone=lvl, reentry=True,
                        entry_time=ts, entry_price=entry_price,
                        qty=qty, capital=capital,
                        tp1=entry_price * (1 + REENTRY_TP_PCT),
                        tp2=0.0,
                        entry_fee=entry_fee, fees=entry_fee,
                    )
                else:
                    pos = Position(
                        zone=lvl, reentry=False,
                        entry_time=ts, entry_price=entry_price,
                        qty=qty, capital=capital,
                        tp1=entry_price * (1 + TP1_PCT),
                        tp2=entry_price * (1 + TP2_PCT),
                        entry_fee=entry_fee, fees=entry_fee,
                    )

                state["positions"].append(pos)
                all_pos.append(pos)

    return all_pos, df


# ── SERIE PNL MARK-TO-MARKET ───────────────────────────────────────────────────
def compute_pnl_series(df: pd.DataFrame, positions: list) -> pd.Series:
    pnl = pd.Series(0.0, index=df.index)

    for p in positions:
        after_entry = df.index >= p.entry_time

        if p.reentry:
            if p.tp1_hit:
                before = after_entry & (df.index < p.exit1_time)
                pnl[before] += p.qty * (df.loc[before, "close"] - p.entry_price) - p.entry_fee
                after  = df.index >= p.exit1_time
                pnl[after] += p.net_pnl
            else:
                pnl[after_entry] += (p.qty * (df.loc[after_entry, "close"] - p.entry_price)
                                     - p.entry_fee)
        else:
            if p.tp1_hit:
                before_tp1 = after_entry & (df.index < p.exit1_time)
                pnl[before_tp1] += (p.qty * (df.loc[before_tp1, "close"] - p.entry_price)
                                    - p.entry_fee)
                realized_tp1 = ((p.qty / 2) * (p.tp1 - p.entry_price)
                                - p.entry_fee
                                - (p.qty / 2) * p.tp1 * MAKER_FEE)
                if p.tp2_hit:
                    between = (df.index >= p.exit1_time) & (df.index < p.exit2_time)
                    pnl[between] += (realized_tp1
                                     + (p.qty / 2) * (df.loc[between, "close"] - p.entry_price))
                    pnl[df.index >= p.exit2_time] += p.net_pnl
                else:
                    after_tp1 = df.index >= p.exit1_time
                    pnl[after_tp1] += (realized_tp1
                                       + (p.qty / 2) * (df.loc[after_tp1, "close"] - p.entry_price))
            else:
                pnl[after_entry] += (p.qty * (df.loc[after_entry, "close"] - p.entry_price)
                                     - p.entry_fee)
    return pnl


# ── REPORTE ────────────────────────────────────────────────────────────────────
def report(positions: list, df: pd.DataFrame) -> pd.DataFrame:
    last_price = df["close"].iloc[-1]
    rows = []

    for p in positions:
        if p.closed:
            net    = p.net_pnl
            status = "CERRADA"
        elif not p.reentry and p.tp1_hit:
            tp1_exit   = (p.qty / 2) * p.tp1 * MAKER_FEE
            realized   = (p.qty / 2) * (p.tp1 - p.entry_price) - p.entry_fee - tp1_exit
            unrealized = (p.qty / 2) * (last_price - p.entry_price)
            net        = realized + unrealized
            status     = "PARCIAL (TP1 ok)"
        else:
            net    = p.qty * (last_price - p.entry_price) - p.entry_fee
            status = "ABIERTA"

        rows.append({
            "Zona":      p.zone,
            "Tipo":      p.tag,
            "Entrada":   p.entry_time.strftime("%Y-%m-%d %H:%M"),
            "Precio E.": round(p.entry_price, 3),
            "TP1":       round(p.tp1, 3),
            "TP2":       round(p.tp2, 3) if not p.reentry else 0,
            "TP1 hit":   "SI" if p.tp1_hit else "NO",
            "TP2 hit":   "SI" if p.tp2_hit else "NO" if not p.reentry else "-",
            "Comis. $":  round(p.fees, 2),
            "PnL neto":  round(net, 2),
            "Estado":    status,
        })

    tdf = pd.DataFrame(rows)

    SEP = "=" * 110
    print(f"\n{SEP}")
    print("  BACKTEST  --  HYPE Support Zone Strategy  (1H)")
    print(f"  Comisiones: taker {TAKER_FEE*100:.2f}% (entradas) | maker {MAKER_FEE*100:.2f}% (TPs)")
    print(SEP)
    print(tdf.to_string(index=False))

    closed  = [p for p in positions if p.closed]
    winners = [p for p in closed if p.net_pnl > 0]

    all_net   = []
    for p in positions:
        if p.closed:
            all_net.append(p.net_pnl)
        elif not p.reentry and p.tp1_hit:
            tp1_exit   = (p.qty / 2) * p.tp1 * MAKER_FEE
            realized   = (p.qty / 2) * (p.tp1 - p.entry_price) - p.entry_fee - tp1_exit
            unrealized = (p.qty / 2) * (last_price - p.entry_price)
            all_net.append(realized + unrealized)
        else:
            all_net.append(p.qty * (last_price - p.entry_price) - p.entry_fee)

    total_net  = sum(all_net)
    total_fees = sum(p.fees for p in positions)
    total_cap  = sum(cfg["capital"] for cfg in ZONES_CFG)

    print(f"\n-- RESUMEN {'-'*70}")
    print(f"  Operaciones totales  : {len(positions)}")
    print(f"  Iniciales / Re-entr. : {sum(1 for p in positions if not p.reentry)} / "
          f"{sum(1 for p in positions if p.reentry)}")
    print(f"  Cerradas             : {len(closed)}")
    if closed:
        print(f"  Win rate (cerradas)  : {len(winners)/len(closed)*100:.1f}%")
    print(f"  PnL neto (c/abiertas): ${total_net:+,.2f}")
    print(f"  Total comisiones     : ${total_fees:.2f}")
    print(f"  Retorno s/ capital   : {total_net/total_cap*100:+.2f}%")
    print(f"  Capital total usado  : ${total_cap:,.0f}")
    print(f"  Ultimo precio        : ${last_price:.3f}")
    print(f"{SEP}\n")

    return tdf


# ── GRAFICOS ───────────────────────────────────────────────────────────────────
def plot(df: pd.DataFrame, positions: list):
    pnl_series = compute_pnl_series(df, positions)

    fig = plt.figure(figsize=(18, 11))
    gs  = gridspec.GridSpec(3, 1, height_ratios=[3, 1, 1], hspace=0.06)
    ax1 = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1], sharex=ax1)
    ax3 = fig.add_subplot(gs[2], sharex=ax1)

    # --- Precio ---
    ax1.plot(df.index, df["close"], color="#1565C0", linewidth=0.7, label="HYPE close")

    colors = ["#2E7D32", "#E65100", "#B71C1C"]
    for cfg, col in zip(ZONES_CFG, colors):
        ax1.axhspan(cfg["low"], cfg["high"], alpha=0.15, color=col)
        ax1.axhline(cfg["low"],  color=col, linewidth=0.6, linestyle="--", alpha=0.7)
        ax1.axhline(cfg["high"], color=col, linewidth=0.6, linestyle="--", alpha=0.7)
        mid = (cfg["low"] + cfg["high"]) / 2
        ax1.text(df.index[20], mid,
                 f"  Z{cfg['level']} ${cfg['low']}-${cfg['high']} | ${cfg['capital']:,.0f}",
                 fontsize=7.5, color=col, va="center", fontweight="bold")

    for p in positions:
        color_e = "#1565C0" if not p.reentry else "#7B1FA2"
        ax1.scatter(p.entry_time, p.entry_price, marker="^", color=color_e,
                    s=80, zorder=6, edgecolors="white", linewidths=0.5)
        if p.tp1_hit:
            ax1.scatter(p.exit1_time, p.tp1, marker="v", color="#2E7D32",
                        s=80, zorder=6, edgecolors="white", linewidths=0.5)
        if not p.reentry and p.tp2_hit:
            ax1.scatter(p.exit2_time, p.tp2, marker="v", color="#66BB6A",
                        s=80, zorder=6, edgecolors="white", linewidths=0.5)

    from matplotlib.lines import Line2D
    legend_h = [
        Line2D([0],[0], marker="^", color="w", markerfacecolor="#1565C0", markersize=9, label="Entrada inicial"),
        Line2D([0],[0], marker="^", color="w", markerfacecolor="#7B1FA2", markersize=9, label="Re-entrada"),
        Line2D([0],[0], marker="v", color="w", markerfacecolor="#2E7D32", markersize=9, label="TP1 / TP re-ent."),
        Line2D([0],[0], marker="v", color="w", markerfacecolor="#66BB6A", markersize=9, label="TP2"),
    ]
    ax1.legend(handles=legend_h, loc="upper right", fontsize=8)
    ax1.set_ylabel("Precio (USDC)", fontsize=9)
    ax1.set_title("HYPE/USDC - Backtest Zonas de Soporte (1H) | Comisiones incluidas",
                  fontsize=12, fontweight="bold")
    ax1.grid(alpha=0.25)

    # --- RSI ---
    ax2.plot(df.index, df["rsi"], color="#E65100", linewidth=0.7)
    ax2.axhline(40, color="#2E7D32", linestyle="--", linewidth=0.9, alpha=0.8, label="RSI 40")
    ax2.axhline(30, color="#B71C1C", linestyle="--", linewidth=0.9, alpha=0.8, label="RSI 30")
    ax2.axhline(70, color="gray",    linestyle=":",  linewidth=0.7, alpha=0.5, label="RSI 70")
    ax2.fill_between(df.index, df["rsi"], 30,
                     where=(df["rsi"] < 30), alpha=0.25, color="#B71C1C")
    ax2.fill_between(df.index, df["rsi"], 40,
                     where=(df["rsi"] < 40), alpha=0.10, color="#E65100")
    ax2.set_ylim(0, 100)
    ax2.set_ylabel("RSI-14", fontsize=9)
    ax2.legend(loc="upper right", fontsize=8)
    ax2.grid(alpha=0.25)

    # --- PnL acumulado ---
    ax3.fill_between(df.index, pnl_series, 0,
                     where=(pnl_series >= 0), color="#2E7D32", alpha=0.35, label="Ganancia")
    ax3.fill_between(df.index, pnl_series, 0,
                     where=(pnl_series < 0),  color="#B71C1C", alpha=0.35, label="Perdida")
    ax3.plot(df.index, pnl_series, color="#1565C0", linewidth=0.8)
    ax3.axhline(0, color="gray", linewidth=0.7)
    ax3.set_ylabel("PnL $ (neto)", fontsize=9)
    ax3.set_xlabel("Fecha", fontsize=9)
    ax3.legend(loc="upper left", fontsize=8)
    ax3.grid(alpha=0.25)

    plt.tight_layout()
    out = Path(__file__).parent.parent / "data" / "backtest_chart.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    print(f"Grafico guardado: {out}")


# ── MAIN ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    df = pd.read_csv(DATA_FILE, index_col="datetime", parse_dates=True)
    print(f"Datos cargados: {len(df)} velas | {df.index[0].date()} al {df.index[-1].date()}")
    print(f"Rango precios:  ${df['low'].min():.2f} -- ${df['high'].max():.2f}\n")

    positions, df_rsi = run_backtest(df)

    if not positions:
        print("Sin operaciones: el precio no visito las zonas con las condiciones RSI requeridas.")
    else:
        report(positions, df_rsi)
        plot(df_rsi, positions)
