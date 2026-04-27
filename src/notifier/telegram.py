# -*- coding: utf-8 -*-
"""
Cliente Telegram para alertas del bot HYPE.
Todos los mensajes usan Markdown de Telegram.
"""
import os
import logging
import time
import requests
from datetime import datetime

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramNotifier:

    def __init__(self, token: str, chat_id: str):
        self.token   = token
        self.chat_id = str(chat_id).strip()
        self._base   = f"https://api.telegram.org/bot{token}"

    @classmethod
    def from_env(cls) -> "TelegramNotifier":
        token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no configurados")
        return cls(token, chat_id)

    def send(self, text: str, silent: bool = False) -> bool:
        try:
            r = requests.post(
                f"{self._base}/sendMessage",
                json={
                    "chat_id":              self.chat_id,
                    "text":                 text,
                    "parse_mode":           "Markdown",
                    "disable_notification": silent,
                },
                timeout=10,
            )
            data = r.json()
            if not data.get("ok"):
                logger.error("Telegram error: %s", data.get("description"))
                return False
            return True
        except Exception as e:
            logger.error("Telegram send failed: %s", e)
            return False

    # ── Mensajes tipificados ───────────────────────────────────────────────────

    def alert_error(self, context: str, error: str) -> bool:
        text = (
            f"⚠️ *Error en el bot HYPE*\n"
            f"Contexto: `{context}`\n"
            f"Error: `{error[:200]}`\n"
            f"`{datetime.now().strftime('%Y-%m-%d %H:%M')} UTC`"
        )
        return self.send(text)

    # ── Grid: mensajes tipificados ────────────────────────────────────────────

    def alert_grid_startup(self, price: float, hype_balance: float,
                            usdc_balance: float, cycles_24h: int,
                            profit_24h: float, orders_placed: list,
                            grid_low: float, grid_high: float) -> bool:
        profit_sign = "+" if profit_24h >= 0 else ""
        placed_line = (f"Órdenes colocadas: `{len(orders_placed)}`\n"
                       if orders_placed else "")
        text = (
            f"*Estado HYPE Grid: 🟢 ACTIVO*\n"
            f"{'─' * 28}\n"
            f"Rango: `${grid_low:.2f} - ${grid_high:.2f}`\n"
            f"HYPE en Inventario: `{hype_balance:.4f}`\n"
            f"USDC Disponible: `{usdc_balance:.2f}`\n"
            f"Grillas Completadas (24h): `{cycles_24h}`\n"
            f"Profit Estimado (24h): `{profit_sign}{profit_24h:.2f} USDC`\n"
            f"{'─' * 28}\n"
            f"Precio actual: `${price:.4f}`\n"
            f"{placed_line}"
            f"`{datetime.now().strftime('%Y-%m-%d %H:%M')} UTC`"
        )
        return self.send(text)

    def alert_grid_buy(self, level: float, price: float,
                        qty: float, sell_price: float) -> bool:
        text = (
            f"✅ *Compra ejecutada — Grid HYPE*\n"
            f"{'─' * 28}\n"
            f"Nivel: `${level:.2f}`\n"
            f"Precio ejecutado: `${price:.4f}`\n"
            f"HYPE comprado: `{qty:.4f}`\n"
            f"Venta colocada en: `${sell_price:.4f}`\n"
            f"`{datetime.now().strftime('%Y-%m-%d %H:%M')} UTC`"
        )
        return self.send(text)

    def alert_grid_sell(self, level: float, buy_price: float,
                         sell_price: float, qty: float,
                         pnl_net: float, total_pnl: float) -> bool:
        pct         = (sell_price - buy_price) / buy_price * 100
        cycle_sign  = "+" if pnl_net  >= 0 else ""
        total_sign  = "+" if total_pnl >= 0 else ""
        text = (
            f"✅ *Venta ejecutada — Grid HYPE*\n"
            f"{'─' * 28}\n"
            f"Nivel: `${level:.2f}`\n"
            f"Compra: `${buy_price:.4f}` → Venta: `${sell_price:.4f}` (+{pct:.2f}%)\n"
            f"Cantidad: `{qty:.4f} HYPE`\n"
            f"{'─' * 28}\n"
            f"*Ganancia del ciclo: `{cycle_sign}{pnl_net:.2f} USDC`*\n"
            f"PnL total acumulado: `{total_sign}{total_pnl:.2f} USDC`\n"
            f"`{datetime.now().strftime('%Y-%m-%d %H:%M')} UTC`"
        )
        return self.send(text)

    def alert_grid_out_of_range(self, direction: str, price: float,
                                  grid_low: float, grid_high: float,
                                  hype_inventory: float,
                                  latent_loss: float | None,
                                  usdc_available: float | None) -> bool:
        if direction == "inferior":
            limit_line   = f"Precio actual: `${price:.4f}` (límite: `${grid_low:.2f}`)"
            extra_line   = f"HYPE en inventario: `{hype_inventory:.4f}`\n"
            extra_line  += f"Pérdida latente estimada: `${latent_loss:.2f}`\n" if latent_loss is not None else ""
            opciones     = "`/reactivar` para continuar | `/shift_down` para mover grilla"
        else:
            limit_line   = f"Precio actual: `${price:.4f}` (límite: `${grid_high:.2f}`)"
            extra_line   = f"USDC disponible: `{usdc_available:.2f}`\n" if usdc_available is not None else ""
            opciones     = "`/reactivar` para continuar | `/shift_up` para mover grilla"
        text = (
            f"🔴 *ALERTA GRID — Precio fuera de rango {'INFERIOR' if direction == 'inferior' else 'SUPERIOR'}*\n"
            f"{'─' * 28}\n"
            f"{limit_line}\n"
            f"Bot pausado automáticamente.\n"
            f"{'─' * 28}\n"
            f"{extra_line}"
            f"Opciones: {opciones}\n"
            f"`{datetime.now().strftime('%Y-%m-%d %H:%M')} UTC`"
        )
        return self.send(text)

    def alert_grid_reactivated(self, price: float) -> bool:
        text = (
            f"✅ *Grid HYPE reactivado*\n"
            f"Precio actual: `${price:.4f}`\n"
            f"Órdenes reconciliadas — grilla activa.\n"
            f"`{datetime.now().strftime('%Y-%m-%d %H:%M')} UTC`"
        )
        return self.send(text)

    def alert_grid_shifted(self, direction: str, new_low: float,
                            new_high: float, price: float,
                            placed: list) -> bool:
        arrow = "⬇️" if direction == "down" else "⬆️"
        text = (
            f"{arrow} *Grilla desplazada — Grid HYPE*\n"
            f"{'─' * 28}\n"
            f"Nuevo rango: `${new_low:.2f} - ${new_high:.2f}`\n"
            f"Precio actual: `${price:.4f}`\n"
            f"Órdenes colocadas: `{len(placed)}`\n"
            f"{'─' * 28}\n"
            f"Grilla activa en nuevo rango.\n"
            f"`{datetime.now().strftime('%Y-%m-%d %H:%M')} UTC`"
        )
        return self.send(text)

    def alert_grid_above_range(self, price: float, grid_high: float) -> bool:
        text = (
            f"⚠️ *HYPE +1.5% por encima de la grilla*\n"
            f"Precio: `${price:.4f}` | Techo: `${grid_high:.2f}`\n"
            f"Considerá `/shift_up` para reposicionar"
        )
        return self.send(text)

    def alert_auto_shift_blocked(self, count: int, max_shifts: int, price: float) -> bool:
        text = (
            f"⛔ *Auto-shift bloqueado — Grid HYPE*\n"
            f"{'─' * 28}\n"
            f"Se alcanzó el límite de `{max_shifts}` shifts automáticos.\n"
            f"Precio actual: `${price:.4f}`\n"
            f"{'─' * 28}\n"
            f"Usá `/shift_up` para mover la grilla manualmente y resetear el contador.\n"
            f"`{datetime.now().strftime('%Y-%m-%d %H:%M')} UTC`"
        )
        return self.send(text)

    def alert_grid_error(self, context: str, error: str) -> bool:
        text = (
            f"❌ *Error en Grid HYPE*\n"
            f"Contexto: `{context}`\n"
            f"Error: `{error[:200]}`\n"
            f"`{datetime.now().strftime('%Y-%m-%d %H:%M')} UTC`"
        )
        return self.send(text)


# ─────────────────────────────────────────────────────────────────────────────


class TelegramCommandPoller:
    """
    Sondea getUpdates cada POLL_INTERVAL segundos y despacha comandos
    (mensajes que empiezan con '/') a los handlers registrados.

    handlers = {"/shift_down": callable(args), "/reactivar": callable(args), …}
    """

    POLL_INTERVAL = 5   # segundos entre cada getUpdates

    def __init__(self, token: str, chat_id: str, handlers: dict):
        self.token    = token
        self.chat_id  = str(chat_id).strip()
        self._base    = f"https://api.telegram.org/bot{token}"
        self.handlers = handlers
        self._offset  = 0

    @classmethod
    def from_notifier(cls, notifier: "TelegramNotifier",
                      handlers: dict) -> "TelegramCommandPoller":
        return cls(notifier.token, notifier.chat_id, handlers)

    def run(self):
        logger.info("TelegramCommandPoller iniciado (poll cada %ds)", self.POLL_INTERVAL)
        self._skip_pending()
        while True:
            try:
                self._poll()
            except Exception as e:
                logger.error("Error en TelegramCommandPoller: %s", e)
            time.sleep(self.POLL_INTERVAL)

    def _skip_pending(self):
        """Drena todos los updates pendientes sin procesarlos y avanza el offset."""
        try:
            r = requests.get(
                f"{self._base}/getUpdates",
                params={"timeout": 0},   # sin offset = todos los pendientes
                timeout=10,
            )
            data = r.json()
            if data.get("ok"):
                results = data.get("result", [])
                if results:
                    self._offset = results[-1]["update_id"] + 1
                logger.info("Poller: offset inicial = %d (%d updates descartados)",
                            self._offset, len(results))
        except Exception as e:
            logger.warning("No se pudo inicializar offset del poller: %s", e)

    def _poll(self):
        try:
            r = requests.get(
                f"{self._base}/getUpdates",
                params={"offset": self._offset, "timeout": 0, "allowed_updates": ["message"]},
                timeout=10,
            )
            data = r.json()
        except Exception as e:
            logger.warning("getUpdates falló: %s", e)
            return

        if not data.get("ok"):
            return

        for update in data.get("result", []):
            self._offset = update["update_id"] + 1
            msg = update.get("message", {})
            # Aceptar solo mensajes del chat configurado
            if str(msg.get("chat", {}).get("id", "")) != self.chat_id:
                continue
            text = msg.get("text", "").strip()
            if not text.startswith("/"):
                continue
            # Separar comando de argumentos (ignorar @botname si lo hay)
            parts   = text.split()
            command = parts[0].split("@")[0].lower()
            args    = parts[1:]
            handler = self.handlers.get(command)
            if handler:
                logger.info("Comando Telegram recibido: %s", command)
                try:
                    handler(args)
                except Exception as e:
                    logger.error("Error ejecutando comando %s: %s", command, e)
            else:
                logger.debug("Comando desconocido: %s", command)
