# -*- coding: utf-8 -*-
"""
Cliente Telegram para alertas del bot HYPE.
Todos los mensajes usan Markdown de Telegram.
"""
import os
import logging
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

    def alert_entry(self, zone: int, price: float, rsi: float,
                    qty: float, capital: float, tp1: float, tp2: float,
                    is_reentry: bool = False) -> bool:
        tag  = "RE-ENTRADA" if is_reentry else "ENTRADA"
        icon = "🔄" if is_reentry else "🟢"
        tp2_line = f"TP2: `${tp2:.3f}` (+4.5%)\n" if not is_reentry else ""
        tp1_label = "TP1: `${:.3f}` (+2.5%)\n".format(tp1) if not is_reentry else f"TP: `${tp1:.3f}` (+1.3%)\n"
        text = (
            f"{icon} *{tag} — Zona {zone} HYPE/USDC*\n"
            f"{'─' * 28}\n"
            f"Precio entrada: `${price:.4f}`\n"
            f"RSI-14: `{rsi:.1f}`\n"
            f"Cantidad: `{qty:.4f} HYPE`\n"
            f"Capital: `${capital:,.0f} USDC`\n"
            f"{'─' * 28}\n"
            f"{tp1_label}"
            f"{tp2_line}"
            f"_Modo: Paper Trading_\n"
            f"`{datetime.now().strftime('%Y-%m-%d %H:%M')} UTC`"
        )
        return self.send(text)

    def alert_tp(self, zone: int, tp_num: int, entry_price: float,
                 tp_price: float, qty: float, pnl: float,
                 fees: float, is_reentry: bool = False) -> bool:
        icon = "✅"
        label = f"TP{tp_num}" if not is_reentry else "TP"
        pct   = (tp_price - entry_price) / entry_price * 100
        text = (
            f"{icon} *{label} ALCANZADO — Zona {zone} HYPE/USDC*\n"
            f"{'─' * 28}\n"
            f"Entrada: `${entry_price:.4f}`\n"
            f"Salida:  `${tp_price:.4f}` (+{pct:.1f}%)\n"
            f"Cantidad cerrada: `{qty:.4f} HYPE`\n"
            f"{'─' * 28}\n"
            f"PnL bruto: `${pnl + fees:+.2f}`\n"
            f"Comisiones: `${fees:.2f}`\n"
            f"*PnL neto: `${pnl:+.2f}`*\n"
            f"_Modo: Paper Trading_\n"
            f"`{datetime.now().strftime('%Y-%m-%d %H:%M')} UTC`"
        )
        return self.send(text)

    def alert_zone_watch(self, price: float, rsi: float,
                         zones_status: list[dict]) -> bool:
        lines = []
        for z in zones_status:
            in_zone = z["in_zone"]
            icon    = "📍" if in_zone else "  "
            lines.append(
                f"{icon} Z{z['level']} `${z['low']:.2f}-${z['high']:.2f}` "
                f"RSI>{z['rsi_entry']} | "
                f"{'EN ZONA' if in_zone else 'fuera'}"
            )
        zones_text = "\n".join(lines)
        text = (
            f"📊 *Monitor HYPE — Resumen horario*\n"
            f"{'─' * 28}\n"
            f"Precio: `${price:.4f}`\n"
            f"RSI-14: `{rsi:.1f}`\n"
            f"{'─' * 28}\n"
            f"{zones_text}\n"
            f"`{datetime.now().strftime('%Y-%m-%d %H:%M')} UTC`"
        )
        return self.send(text, silent=True)

    def alert_error(self, context: str, error: str) -> bool:
        text = (
            f"⚠️ *Error en el bot HYPE*\n"
            f"Contexto: `{context}`\n"
            f"Error: `{error[:200]}`\n"
            f"`{datetime.now().strftime('%Y-%m-%d %H:%M')} UTC`"
        )
        return self.send(text)

    def alert_startup(self, network: str, zones: list[dict], price: float) -> bool:
        zone_lines = "\n".join(
            f"  Z{z['level']} `${z['low']:.2f}-${z['high']:.2f}` "
            f"RSI>{z['rsi_entry']} cap `${z['capital']:,.0f}`"
            for z in zones
        )
        text = (
            f"🚀 *HYPE Bot iniciado*\n"
            f"{'─' * 28}\n"
            f"Red: `{network}`\n"
            f"Modo: `Paper Trading`\n"
            f"Precio actual: `${price:.4f}`\n"
            f"{'─' * 28}\n"
            f"*Zonas monitoreadas:*\n"
            f"{zone_lines}\n"
            f"{'─' * 28}\n"
            f"Checkeando cada hora al cierre de vela 1H\n"
            f"`{datetime.now().strftime('%Y-%m-%d %H:%M')} UTC`"
        )
        return self.send(text)
