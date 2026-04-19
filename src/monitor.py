# -*- coding: utf-8 -*-
"""
Monitor principal: corre un ciclo de estrategia cada hora al cierre de vela 1H.
Modo paper trading — no ejecuta ordenes reales.

Uso:
    python src/monitor.py            # corre + espera proxima vela
    python src/monitor.py --now      # corre un ciclo ahora mismo y sale
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
import time
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
load_dotenv()

from src.strategies.live_zones import LiveZoneStrategy, ZONES_CFG, load_state
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
    now     = datetime.now(timezone.utc)
    next_h  = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return (next_h - now).total_seconds() + 5


def run_cycle(strategy: LiveZoneStrategy, notifier: TelegramNotifier,
              send_summary: bool = True):
    """Ejecuta un ciclo completo y envia alertas Telegram segun eventos."""
    logger.info("Iniciando ciclo de estrategia...")
    try:
        events = strategy.run_cycle()
    except Exception as e:
        logger.error("Error en ciclo: %s", e)
        notifier.alert_error("run_cycle", str(e))
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
        )

    # ── Resumen horario (silencioso si no hubo eventos) ───────────────────
    if send_summary:
        notifier.alert_zone_watch(price, rsi, zones)

    # ── Log de posiciones ─────────────────────────────────────────────────
    logger.info(strategy.summary(price))
    logger.info(
        "Ciclo #%d completo | precio=$%.4f rsi=%.1f | "
        "entradas=%d tp_hits=%d",
        strategy.state.run_count,
        price, rsi, len(entries), len(tp_hits),
    )


def show_status():
    """Muestra el estado actual de posiciones sin correr un ciclo."""
    state = load_state()
    open_pos = [p for p in state.positions if not (
        p.get("tp1_hit") if p.get("reentry") else p.get("tp2_hit")
    )]
    closed_pos = [p for p in state.positions if p not in open_pos]

    SEP = "=" * 55
    print(f"\n{SEP}")
    print(f"  Estado actual — HYPE Paper Trading")
    print(f"  Ultimo ciclo:  {state.last_run or 'nunca'}")
    print(f"  Ciclos totales: {state.run_count}")
    print(SEP)

    print(f"\nPosiciones ABIERTAS: {len(open_pos)}")
    for p in open_pos:
        tp1_s = "OK" if p.get("tp1_hit") else "pendiente"
        print(f"  Z{p['zone']} {p['tag'] if hasattr(p,'tag') else p.get('reentry','?')} | "
              f"px=${p['entry_price']:.4f} | tp1={tp1_s} | "
              f"entrada: {p['entry_time'][:16]}")

    print(f"\nPosiciones CERRADAS: {len(closed_pos)}")
    for p in closed_pos:
        print(f"  Z{p['zone']} | pnl_neto=${p['gross_pnl'] - p['fees']:+.2f} | "
              f"entrada: {p['entry_time'][:16]}")

    print(f"\nPnL neto total cerrado: ${state.total_net_pnl:+.2f}")
    print(f"{SEP}\n")


def main():
    parser = argparse.ArgumentParser(description="HYPE Zone Monitor")
    parser.add_argument("--now",    action="store_true",
                        help="Ejecutar un ciclo ahora y salir")
    parser.add_argument("--status", action="store_true",
                        help="Mostrar estado de posiciones y salir")
    parser.add_argument("--no-summary", action="store_true",
                        help="No enviar resumen horario por Telegram")
    args = parser.parse_args()

    if args.status:
        show_status()
        return

    notifier = TelegramNotifier.from_env()
    strategy = LiveZoneStrategy()

    if args.now:
        logger.info("Modo --now: ejecutando ciclo unico")
        run_cycle(strategy, notifier, send_summary=not args.no_summary)
        return

    # ── Loop continuo ─────────────────────────────────────────────────────
    logger.info("Monitor iniciado en modo paper trading")

    # Mensaje de arranque
    try:
        from src.exchanges.hyperliquid_client import HyperliquidClient
        hl = HyperliquidClient.from_env()
        price = hl.get_mid_price("HYPE")
        notifier.alert_startup(
            network=os.getenv("HYPERLIQUID_NETWORK", "mainnet"),
            zones=ZONES_CFG,
            price=price,
        )
    except Exception as e:
        logger.warning("No se pudo enviar startup alert: %s", e)

    while True:
        run_cycle(strategy, notifier, send_summary=not args.no_summary)

        wait = seconds_to_next_candle()
        next_time = datetime.now(timezone.utc) + timedelta(seconds=wait)
        logger.info(
            "Proximo ciclo en %.0f min | %s UTC",
            wait / 60,
            next_time.strftime("%H:%M"),
        )
        time.sleep(wait)


if __name__ == "__main__":
    main()
