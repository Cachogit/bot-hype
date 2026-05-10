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

import json
import logging
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import math

from dotenv import load_dotenv
load_dotenv()

from src.exchanges.hyperliquid_client import HyperliquidClient
from src.exchanges.hyperliquid_ws import HyperliquidWS
from src.notifier.telegram import TelegramNotifier, TelegramCommandPoller
from src.strategies.grid import GridStrategy
from config.grid_config import ASSET

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

    def cmd_reset_grid(_args):
        price = client.get_mid_price(ASSET)
        result = grid.reset_grid(price)
        notifier.send(
            f"🔄 *Grid reseteada*\n"
            f"Niveles a IDLE, PnL en $0\n"
            f"Órdenes colocadas: `{len(result['placed'])}`\n"
            f"Precio actual: `${price:.4f}`"
        )

    def cmd_pnl(_args):
        pnl_file = Path(ROOT_DIR) / "data" / "pnl_history.json"
        if not pnl_file.exists():
            notifier.send("📊 *PnL Historial*\nAún no hay ciclos completados.")
            return
        try:
            history = json.loads(pnl_file.read_text(encoding="utf-8"))
        except Exception:
            notifier.send("Error leyendo historial de PnL.")
            return

        if not history:
            notifier.send("📊 *PnL Historial*\nAún no hay ciclos completados.")
            return

        now = datetime.now(timezone.utc)

        def pnl_since(days):
            cutoff = now.timestamp() - days * 86400
            entries = [
                e for e in history
                if datetime.strptime(e["timestamp"], "%Y-%m-%dT%H:%M:%SZ")
                   .replace(tzinfo=timezone.utc).timestamp() >= cutoff
            ]
            return sum(e["pnl_net"] for e in entries), len(entries)

        total_pnl    = round(sum(e["pnl_net"] for e in history), 2)
        total_ciclos = len(history)
        pnl_7d,  ciclos_7d  = pnl_since(7)
        pnl_30d, ciclos_30d = pnl_since(30)

        notifier.send(
            f"📊 *PnL Historial*\n"
            f"─────────────────\n"
            f"Total acumulado: `${total_pnl:.2f}`\n"
            f"Ciclos totales:  `{total_ciclos}`\n"
            f"─────────────────\n"
            f"Últimos 7 días:\n"
            f"  Ganancia: `${pnl_7d:.2f}` | Ciclos: `{ciclos_7d}`\n"
            f"Últimos 30 días:\n"
            f"  Ganancia: `${pnl_30d:.2f}` | Ciclos: `{ciclos_30d}`"
        )

    return {
        "/status":     cmd_status,
        "/shift_down": cmd_shift_down,
        "/shift_up":   cmd_shift_up,
        "/reactivar":  cmd_reactivar,
        "/reset_grid": cmd_reset_grid,
        "/pnl":        cmd_pnl,
    }


REBALANCE_FILE    = Path(ROOT_DIR) / "data" / "last_rebalance.json"
REBALANCE_DAYS    = 7
REBALANCE_RESERVE = 0.95   # usar 95% del USDC disponible


def _weekly_rebalance_loop(grid, client, notifier):
    from config.grid_config import N_LEVELS
    while True:
        time.sleep(3600)  # verificar cada hora

        # ¿Pasaron 7 días desde el último rebalanceo?
        if REBALANCE_FILE.exists():
            try:
                data      = json.loads(REBALANCE_FILE.read_text(encoding="utf-8"))
                last_ts   = datetime.fromisoformat(data["last_rebalance"])
                elapsed   = (datetime.now(timezone.utc) - last_ts).total_seconds()
                if elapsed < REBALANCE_DAYS * 86400:
                    continue
            except Exception:
                pass

        # ¿Todas las órdenes son de compra esperando? (ninguna venta pendiente)
        if not grid.all_waiting_buy():
            continue

        usdc        = client.get_usdc_balance()
        new_capital = math.floor((usdc * REBALANCE_RESERVE) / N_LEVELS)
        old_capital = grid.state["capital_per_level"]

        if new_capital <= old_capital:
            logger.info("Rebalanceo semanal: capital nuevo ($%.0f) no supera actual ($%.0f) — omitido",
                        new_capital, old_capital)
            # Guardar igual para no volver a intentar hasta la próxima semana
            REBALANCE_FILE.write_text(json.dumps({
                "last_rebalance": datetime.now(timezone.utc).isoformat()
            }), encoding="utf-8")
            continue

        price = client.get_mid_price(ASSET)
        grid.rebalance_capital(new_capital, price)

        REBALANCE_FILE.write_text(json.dumps({
            "last_rebalance": datetime.now(timezone.utc).isoformat()
        }), encoding="utf-8")

        notifier.send(
            f"📈 *Rebalanceo semanal*\n"
            f"USDC disponible: `${usdc:.2f}`\n"
            f"Capital por nivel: `${old_capital:.0f}` → `${new_capital:.0f}`\n"
            f"Grid reseteada con nuevo capital"
        )
        logger.info("Rebalanceo semanal completado: $%.0f → $%.0f", old_capital, new_capital)


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

    # ── Rebalanceo semanal ────────────────────────────────────────────────────
    t_rebalance = threading.Thread(
        target=_weekly_rebalance_loop, args=(grid, client, notifier), daemon=True
    )
    t_rebalance.start()
    logger.info("Thread de rebalanceo semanal iniciado")

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
