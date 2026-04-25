# -*- coding: utf-8 -*-
"""
Monitor principal: chequea precio y RSI cada 2 minutos en tiempo real.

Cada ciclo:
  1. Obtiene precio mid actual del exchange.
  2. Calcula RSI sobre velas CERRADAS de 1H y 15m.
  3. Detecta cruce de RSI hacia arriba (40 o 30) en las ultimas 2 velas cerradas.
  4. Si precio esta en zona Y hay cruce → ejecuta entrada.

Uso:
    python src/monitor.py            # loop cada 2 minutos
    python src/monitor.py --now      # un ciclo y sale
    python src/monitor.py --status   # muestra estado de posiciones y sale
"""
import sys, io, os

ROOT_DIR = os.path.join(os.path.dirname(__file__), "..")
os.makedirs(os.path.join(ROOT_DIR, "logs"), exist_ok=True)
os.makedirs(os.path.join(ROOT_DIR, "data"), exist_ok=True)

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ROOT_DIR)

import argparse
import logging
import time
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
load_dotenv()

from src.strategies.live_zones import (LiveZoneStrategy, ZONES_CFG, load_state,
                                        STATE_FILE, STATE_FILE_15M)
from src.notifier.telegram import TelegramNotifier
from src.exchanges.hyperliquid_client import HyperliquidClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/monitor.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("monitor")

CHECK_INTERVAL  = 120   # segundos entre cada chequeo
SUMMARY_EVERY   = 30    # enviar resumen cada N ciclos (~1 hora)


def run_cycle(strategy: LiveZoneStrategy, notifier: TelegramNotifier,
              current_price: float, send_summary: bool = False):
    """Ejecuta un ciclo RT y envia alertas Telegram segun eventos."""
    tf = strategy.timeframe.upper()
    logger.info("Iniciando ciclo RT [%s] | precio=%.4f", tf, current_price)
    try:
        events = strategy.run_realtime_cycle(current_price)
    except Exception as e:
        logger.error("Error en ciclo RT [%s]: %s", tf, e)
        notifier.alert_error(f"run_realtime_cycle [{tf}]", str(e))
        return

    price    = events["price"]
    rsi      = events["rsi"]
    live_rsi = events["live_rsi"]
    entries  = events["entries"]
    tp_hits  = events["tp_hits"]
    zones    = events["zones_status"]

    # ── Alertas de TP ─────────────────────────────────────────────────
    for hit in tp_hits:
        pos = hit["pos"]
        notifier.alert_tp(
            zone=pos.zone,
            tp_num=hit["tp_num"],
            entry_price=pos.entry_price,
            tp_price=hit["tp_price"],
            qty=hit["qty"],
            pnl=hit["pnl"],
            fees=hit["fees"],
            is_reentry=pos.reentry,
            timeframe=tf,
        )

    # ── Alertas de entrada ────────────────────────────────────────────
    for entry in entries:
        pos = entry["pos"]
        notifier.alert_entry(
            zone=pos.zone,
            price=pos.entry_price,
            rsi=entry["rsi"],
            qty=pos.qty,
            capital=pos.capital,
            tp1=pos.tp1,
            tp2=pos.tp2,
            is_reentry=pos.reentry,
            timeframe=tf,
        )

    # ── Resumen periodico ─────────────────────────────────────────────
    if send_summary:
        notifier.alert_zone_watch(price, rsi, zones, timeframe=tf)

    logger.info(
        "Ciclo RT #%d [%s] | precio=$%.4f rsi_cerrado=%.1f rsi_live=%.1f | entradas=%d tp_hits=%d",
        strategy.state.run_count, tf,
        price, rsi, live_rsi, len(entries), len(tp_hits),
    )
    logger.info(strategy.summary(price))


def _print_state_block(state, label: str):
    SEP = "=" * 55
    open_pos = [p for p in state.positions if not (
        p.get("tp1_hit") if p.get("reentry") else p.get("tp2_hit")
    )]
    closed_pos = [p for p in state.positions if p not in open_pos]
    print(f"\n{SEP}")
    print(f"  Estado actual — HYPE Paper Trading [{label}]")
    print(f"  Ultimo ciclo:  {state.last_run or 'nunca'}")
    print(f"  Ciclos totales: {state.run_count}")
    print(SEP)
    print(f"\nPosiciones ABIERTAS: {len(open_pos)}")
    for p in open_pos:
        tp1_s = "OK" if p.get("tp1_hit") else "pendiente"
        print(f"  Z{p['zone']} {'RE-ENTRY' if p.get('reentry') else 'INICIAL'} | "
              f"px=${p['entry_price']:.4f} | tp1={tp1_s} | "
              f"entrada: {p['entry_time'][:16]}")
    print(f"\nPosiciones CERRADAS: {len(closed_pos)}")
    for p in closed_pos:
        print(f"  Z{p['zone']} | pnl_neto=${p['gross_pnl'] - p['fees']:+.2f} | "
              f"entrada: {p['entry_time'][:16]}")
    print(f"\nPnL neto total cerrado: ${state.total_net_pnl:+.2f}")
    print(f"{SEP}\n")


def show_status():
    """Muestra el estado actual de posiciones sin correr un ciclo."""
    _print_state_block(load_state(STATE_FILE),    "1H")
    _print_state_block(load_state(STATE_FILE_15M), "15m")


def main():
    parser = argparse.ArgumentParser(description="HYPE Zone Monitor — chequeo cada 2min")
    parser.add_argument("--now",        action="store_true",
                        help="Ejecutar un ciclo ahora y salir")
    parser.add_argument("--status",     action="store_true",
                        help="Mostrar estado de posiciones y salir")
    parser.add_argument("--no-summary", action="store_true",
                        help="No enviar resumen periodico por Telegram")
    args = parser.parse_args()

    if args.status:
        show_status()
        return

    notifier     = TelegramNotifier.from_env()
    strategy_1h  = LiveZoneStrategy(timeframe="1h")
    strategy_15m = LiveZoneStrategy(timeframe="15m")
    hl           = HyperliquidClient.from_env()

    if args.now:
        logger.info("Modo --now: ejecutando ciclo unico RT 1H + 15m")
        price = hl.get_mid_price("HYPE")
        run_cycle(strategy_1h,  notifier, price, send_summary=True)
        run_cycle(strategy_15m, notifier, price, send_summary=False)
        return

    # ── Startup ───────────────────────────────────────────────────────
    try:
        price = hl.get_mid_price("HYPE")
        notifier.alert_startup(
            network=os.getenv("HYPERLIQUID_NETWORK", "mainnet"),
            zones=ZONES_CFG,
            price=price,
        )
    except Exception as e:
        logger.warning("No se pudo enviar startup alert: %s", e)

    logger.info(
        "Monitor RT iniciado — chequeo cada %ds | resumen cada %d ciclos (~%dmin)",
        CHECK_INTERVAL, SUMMARY_EVERY, CHECK_INTERVAL * SUMMARY_EVERY // 60,
    )

    cycle_num = 0
    while True:
        cycle_num += 1
        send_summary = (not args.no_summary) and (cycle_num % SUMMARY_EVERY == 0)

        try:
            price = hl.get_mid_price("HYPE")
        except Exception as e:
            logger.error("Error obteniendo precio: %s — saltando ciclo", e)
            time.sleep(CHECK_INTERVAL)
            continue

        run_cycle(strategy_1h,  notifier, price, send_summary=send_summary)
        run_cycle(strategy_15m, notifier, price, send_summary=False)

        next_time = datetime.now(timezone.utc) + timedelta(seconds=CHECK_INTERVAL)
        logger.info(
            "Ciclo %d completo | proximo chequeo a las %s UTC",
            cycle_num, next_time.strftime("%H:%M:%S"),
        )
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
