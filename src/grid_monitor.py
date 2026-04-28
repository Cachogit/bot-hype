# -*- coding: utf-8 -*-
"""
Grid Monitor — entry point del bot de grid trading HYPE/USDC.

Flujo de arranque:
  1. Reconciliar estado vs órdenes reales en Hyperliquid
  2. Notificar Telegram con resumen inicial
  3. Iniciar poller de comandos Telegram en hilo daemon
  4. Escuchar fills y precio vía WebSocket (bloquea main thread)

Comandos Telegram aceptados:
  /status      — resumen del estado actual
  /shift_down  — mover grilla hacia abajo centrada en precio actual
  /shift_up    — mover grilla hacia arriba centrada en precio actual
  /reactivar   — reactivar si está pausado manualmente
"""
import sys, io, os

ROOT_DIR = os.path.join(os.path.dirname(__file__), "..")
os.makedirs(os.path.join(ROOT_DIR, "logs"), exist_ok=True)
os.makedirs(os.path.join(ROOT_DIR, "data"), exist_ok=True)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ROOT_DIR)

import logging
import signal
import threading
import time

from dotenv import load_dotenv
load_dotenv()

from src.exchanges.hyperliquid_client import HyperliquidClient
from src.exchanges.hyperliquid_ws import HyperliquidWS
from src.notifier.telegram import TelegramNotifier, TelegramCommandPoller
from src.strategies.grid import GridStrategy
from config.grid_config import ASSET, GRID_LOW, GRID_HIGH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/grid.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("grid_monitor")


def _build_command_handlers(grid: GridStrategy,
                             client: HyperliquidClient,
                             notifier: TelegramNotifier) -> dict:
    def cmd_status(_args):
        price    = client.get_mid_price(ASSET)
        stats    = grid.stats_24h()
        hype_bal = client.get_coin_balance(ASSET)
        usdc_bal = client.get_usdc_balance()
        notifier.alert_grid_startup(
            price=price,
            hype_balance=hype_bal,
            usdc_balance=usdc_bal,
            cycles_24h=stats["cycles"],
            profit_24h=stats["profit"],
            orders_placed=[],
            grid_low=grid.grid_low,
            grid_high=grid.grid_high,
        )

    def cmd_shift_down(_args):
        price = client.get_mid_price(ASSET)
        grid.shift("down", price)

    def cmd_shift_up(_args):
        price = client.get_mid_price(ASSET)
        grid.shift("up", price)

    def cmd_reactivar(_args):
        price = client.get_mid_price(ASSET)
        grid.reactivar(price)

    return {
        "/status":     cmd_status,
        "/shift_down": cmd_shift_down,
        "/shift_up":   cmd_shift_up,
        "/reactivar":  cmd_reactivar,
    }


def _make_shutdown_handler(client: HyperliquidClient, notifier: TelegramNotifier):
    def _handler(signum, _frame):
        sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
        logger.info("Señal %s recibida — iniciando shutdown limpio", sig_name)
        try:
            cancelled = client.cancel_all_orders(ASSET)
            logger.info("Shutdown: %d órdenes canceladas", len(cancelled))
            notifier.alert_grid_shutdown(cancelled=len(cancelled))
        except Exception as e:
            logger.error("Error durante shutdown: %s", e)
        sys.exit(0)
    return _handler


def main():
    time.sleep(10)  # esperar que Railway termine de levantar el entorno
    client   = HyperliquidClient.from_env()
    notifier = TelegramNotifier.from_env()
    grid     = GridStrategy(client, notifier)

    shutdown = _make_shutdown_handler(client, notifier)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT,  shutdown)

    # ── 1. Reconciliar ────────────────────────────────────────────────────────
    price = client.get_mid_price(ASSET)
    logger.info("Precio actual %s: $%.4f", ASSET, price)

    result = grid.reconcile(price)
    logger.info(
        "Reconciliación: %d colocados | %d restaurados | %d errores | %d cap-skip",
        len(result["placed"]), len(result["restored"]),
        len(result["errors"]), len(result.get("skipped", [])),
    )

    # ── 2. Notificación de inicio ─────────────────────────────────────────────
    stats    = grid.stats_24h()
    hype_bal = client.get_coin_balance(ASSET)
    usdc_bal = client.get_usdc_balance()
    notifier.alert_grid_startup(
        price=price,
        hype_balance=hype_bal,
        usdc_balance=usdc_bal,
        cycles_24h=stats["cycles"],
        profit_24h=stats["profit"],
        orders_placed=result["placed"],
        grid_low=grid.grid_low,
        grid_high=grid.grid_high,
    )

    # ── 3. Poller de comandos Telegram ────────────────────────────────────────
    handlers = _build_command_handlers(grid, client, notifier)
    poller   = TelegramCommandPoller.from_notifier(notifier, handlers)
    t = threading.Thread(target=poller.run, daemon=True)
    t.start()
    logger.info("Poller de comandos Telegram iniciado")

    # ── 4. WebSocket ──────────────────────────────────────────────────────────
    network = os.getenv("HYPERLIQUID_NETWORK", "mainnet")
    ws = HyperliquidWS(
        address=client.subaccount,
        on_fill=grid.on_fill,
        on_price=grid.on_price,
        coin=ASSET,
        network=network,
    )
    logger.info("Iniciando WebSocket — escuchando fills y precio en tiempo real")
    ws.run_forever()


if __name__ == "__main__":
    main()
