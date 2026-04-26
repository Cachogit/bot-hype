# -*- coding: utf-8 -*-
"""
WebSocket de Hyperliquid: suscribe a userFills (fills propios) y
allMids (precio en tiempo real para detección de rango).
Reconexión automática con backoff exponencial.
"""
import logging
import time

from hyperliquid.info import Info
from hyperliquid.utils import constants

logger = logging.getLogger(__name__)

_RECONNECT_BASE = 5
_RECONNECT_MAX  = 120


class HyperliquidWS:

    def __init__(self, address: str, on_fill, on_price=None,
                 coin: str = "HYPE", network: str = "mainnet"):
        self.address  = address.lower()
        self.on_fill  = on_fill
        self.on_price = on_price
        self.coin     = coin.upper()
        self.network  = network
        self._spot_id = coin.upper()   # puede ser "@N" para spot
        self._info    = None
        self._last_price: float = 0.0

    def _base_url(self) -> str:
        return (constants.MAINNET_API_URL if self.network == "mainnet"
                else constants.TESTNET_API_URL)

    # ── Conexión ──────────────────────────────────────────────────────────────

    def connect(self):
        self._info = Info(base_url=self._base_url(), skip_ws=False)

        # Resolver ID de HYPE en spot (puede ser "@N")
        try:
            mids = self._info.all_mids()
            if self.coin not in mids:
                meta = self._info.spot_meta()
                for token in meta.get("tokens", []):
                    if token["name"].upper() == self.coin:
                        self._spot_id = f"@{token['index']}"
                        break
            else:
                self._spot_id = self.coin
        except Exception as e:
            logger.warning("No se pudo resolver spot_id para %s: %s", self.coin, e)

        self._info.subscribe(
            {"type": "userFills", "user": self.address},
            self._handle_fills,
        )
        if self.on_price is not None:
            self._info.subscribe({"type": "allMids"}, self._handle_mids)

        logger.info("WS conectado | addr=%s… | coin=%s (%s)",
                    self.address[:10], self.coin, self._spot_id)

    # ── Handlers ──────────────────────────────────────────────────────────────

    def _handle_fills(self, msg: dict):
        if msg.get("channel") != "userFills":
            return
        data = msg.get("data", {})
        if data.get("isSnapshot"):
            return
        for fill in data.get("fills", []):
            coin = fill.get("coin", "")
            if coin.upper() in (self.coin, self._spot_id.upper()):
                try:
                    self.on_fill(fill)
                except Exception as e:
                    logger.error("Error en on_fill: %s | fill=%s", e, fill)

    def _handle_mids(self, msg: dict):
        if msg.get("channel") != "allMids":
            return
        mids = msg.get("data", {}).get("mids", {})
        price_str = mids.get(self.coin) or mids.get(self._spot_id)
        if price_str is None:
            return
        try:
            price = float(price_str)
        except (ValueError, TypeError):
            return
        # Debounce: solo propagar si el precio cambió ≥ $0.01
        if abs(price - self._last_price) < 0.01:
            return
        self._last_price = price
        try:
            self.on_price(price)
        except Exception as e:
            logger.error("Error en on_price: %s | price=%s", e, price)

    # ── Loop principal con reconexión ─────────────────────────────────────────

    def run_forever(self):
        """
        El SDK de Hyperliquid inicia el WebSocket en un hilo daemon propio
        (WebsocketManager). No expone run_forever() en Info.
        Este método conecta, luego monitorea que el hilo daemon siga vivo
        y reconecta con backoff si muere.
        """
        delay = _RECONNECT_BASE
        while True:
            try:
                self.connect()
                ws_mgr = getattr(self._info, "ws_manager", None)
                if ws_mgr is None:
                    raise RuntimeError("ws_manager no disponible en Info — verificar versión del SDK")
                logger.info("WS daemon corriendo (hilo=%s)", ws_mgr.name)
                # Monitorear que el hilo daemon siga vivo
                while ws_mgr.is_alive():
                    time.sleep(5)
                logger.warning("WS daemon terminó — reconectando en %ds", delay)
            except Exception as e:
                logger.error("WS error: %s — reconectando en %ds", e, delay)
            time.sleep(delay)
            delay = min(delay * 2, _RECONNECT_MAX)
