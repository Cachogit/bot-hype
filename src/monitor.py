# -*- coding: utf-8 -*-
"""
Monitor principal: corre ciclos de estrategia en paralelo para 1H y 15m.
Modo paper trading — no ejecuta ordenes reales.

Uso:
    python src/monitor.py            # corre ambos timeframes en loop
    python src/monitor.py --now      # corre un ciclo de cada TF y sale
    python src/monitor.py --status   # muestra estado de posiciones y sale
"""
import sys, io, os

# Crear directorios necesarios antes de cualquier import que los use
ROOT_DIR = os.path.join(os.path.dirname(__file__), "..")
os.makedirs(os.path.join(ROOT_DIR, "logs"), exist_ok=True)
os.makedirs(os.path.join(ROOT_DIR, "data"), exist_ok=True)

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ROOT_DIR)

import argparse
import logging
import threading
import time
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
load_dotenv()

from src.strategies.live_zones import (LiveZoneStrategy, ZONES_CFG, load_state,
                                        STATE_FILE, STATE_FILE_15M)
from src.notifier.telegram import TelegramNotifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/monitor.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("monitor")


def seconds_to_next_candle() -> float:
    """Segundos hasta el proximo cierre de vela 1H (mas 5 segundos de margen)."""
    now    = datetime.now(timezone.utc)
    next_h = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return (next_h - now).total_seconds() + 5


def seconds_to_next_15m_candle() -> float:
    """Segundos hasta el proximo cierre de vela 15m (mas 5 segundos de margen)."""
    now          = datetime.now(timezone.utc)
    total_secs   = now.minute * 60 + now.second + now.microsecond / 1e6
    secs_in_slot = total_secs % 900          # 900s = 15 min
    return 900 - secs_in_slot + 5


def run_cycle(strategy: LiveZoneStrategy, notifier: TelegramNotifier,
              send_summary: bool = True):
    """Ejecuta un ciclo completo y envia alertas Telegram segun eventos."""
    tf = strategy.timeframe.upper()
    logger.info("Iniciando ciclo de estrategia [%s]...", tf)
    try:
        events = strategy.run_cycle()
    except Exception as e:
        logger.error("Error en ciclo [%s]: %s", tf, e)
        notifier.alert_error(f"run_cycle [{tf}]", str(e))
        return

    price   = events["price"]
    rsi     = events["rsi"]
    entries = events["entries"]
    tp_hits = events["tp_hits"]
    zones   = events["zones_status"]

    # ── Alertas de TP ─────────────────────────────────────────────────────
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

    # ── Alertas de entrada ────────────────────────────────────────────────
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

    # ── Resumen (silencioso si no hubo eventos) ───────────────────────────
    if send_summary:
        notifier.alert_zone_watch(price, rsi, zones, timeframe=tf)

    # ── Log de posiciones ─────────────────────────────────────────────────
    logger.info(strategy.summary(price))
    logger.info(
        "Ciclo #%d [%s] completo | precio=$%.4f rsi=%.1f | "
        "entradas=%d tp_hits=%d",
        strategy.state.run_count, tf,
        price, rsi, len(entries), len(tp_hits),
    )


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


def _loop(strategy: LiveZoneStrategy, notifier: TelegramNotifier,
          wait_fn, send_summary: bool):
    """Loop continuo para un timeframe dado. Corre en su propio hilo."""
    tf = strategy.timeframe.upper()
    while True:
        run_cycle(strategy, notifier, send_summary=send_summary)
        wait      = wait_fn()
        next_time = datetime.now(timezone.utc) + timedelta(seconds=wait)
        logger.info(
            "[%s] Proximo ciclo en %.0f min | %s UTC",
            tf, wait / 60, next_time.strftime("%H:%M"),
        )
        time.sleep(wait)


def main():
    parser = argparse.ArgumentParser(description="HYPE Zone Monitor")
    parser.add_argument("--now",    action="store_true",
                        help="Ejecutar un ciclo de cada TF ahora y salir")
    parser.add_argument("--status", action="store_true",
                        help="Mostrar estado de posiciones y salir")
    parser.add_argument("--no-summary", action="store_true",
                        help="No enviar resumen periodico por Telegram")
    args = parser.parse_args()

    if args.status:
        show_status()
        return

    notifier    = TelegramNotifier.from_env()
    strategy_1h  = LiveZoneStrategy(timeframe="1h")
    strategy_15m = LiveZoneStrategy(timeframe="15m")

    if args.now:
        logger.info("Modo --now: ejecutando ciclo unico 1H + 15m")
        run_cycle(strategy_1h,  notifier, send_summary=not args.no_summary)
        run_cycle(strategy_15m, notifier, send_summary=not args.no_summary)
        return

    # ── Loops continuos en paralelo ───────────────────────────────────────
    logger.info("Monitor iniciado: 1H + 15m en paralelo")

    try:
        from src.exchanges.hyperliquid_client import HyperliquidClient
        hl    = HyperliquidClient.from_env()
        price = hl.get_mid_price("HYPE")
        notifier.alert_startup(
            network=os.getenv("HYPERLIQUID_NETWORK", "mainnet"),
            zones=ZONES_CFG,
            price=price,
        )
    except Exception as e:
        logger.warning("No se pudo enviar startup alert: %s", e)

    # 15m corre en un hilo daemon — muere si el proceso principal termina
    t15 = threading.Thread(
        target=_loop,
        args=(strategy_15m, notifier, seconds_to_next_15m_candle, False),
        daemon=True,
        name="monitor-15m",
    )
    t15.start()

    # 1H corre en el hilo principal
    _loop(strategy_1h, notifier, seconds_to_next_candle, not args.no_summary)


if __name__ == "__main__":
    main()
