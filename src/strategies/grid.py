# -*- coding: utf-8 -*-
"""
Estrategia de grid trading para HYPE/USDC.

Máquina de estados por nivel:
  IDLE → WAITING_BUY → WAITING_SELL → IDLE → …

Manejo de rango:
  - Precio fuera de rango → pausa + alerta Telegram
  - Precio vuelve al rango antes de acción del usuario → reactivación automática
  - /shift_down / /shift_up → cancela todas, recalcula grilla, recoloca órdenes
"""
import json
import logging
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from config.grid_config import (
    ASSET, CAPITAL_USDC, MAX_CAPITAL_USDC, MAKER_FEE,
    LEVEL_SPACING, LEVELS, GRID_LOW, GRID_HIGH,
)

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).parent.parent.parent / "data" / "grid_state.json"

IDLE         = "IDLE"
WAITING_BUY  = "WAITING_BUY"
WAITING_SELL = "WAITING_SELL"

N_SHIFT_LEVELS = 20   # niveles totales al hacer shift


def _empty_level() -> dict:
    return {
        "status":        IDLE,
        "buy_order_id":  None,
        "sell_order_id": None,
        "buy_price":     None,
        "sell_price":    None,
        "qty":           0.0,
        "expected_qty":  0.0,
    }


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("grid_state.json inválido: %s — empezando limpio", e)
    levels = {str(lvl): _empty_level() for lvl in LEVELS}
    return {
        "grid_low":           GRID_LOW,
        "grid_high":          GRID_HIGH,
        "levels":             levels,
        "completed_cycles":   [],
        "total_realized_pnl": 0.0,
        "paused":             False,
        "pause_reason":       None,
    }


def _save_state(state: dict):
    STATE_FILE.write_text(
        json.dumps(state, indent=2, default=str),
        encoding="utf-8",
    )


# ─────────────────────────────────────────────────────────────────────────────


class GridStrategy:

    def __init__(self, client, notifier):
        self.client   = client
        self.notifier = notifier
        self._lock    = threading.Lock()
        self.state    = _load_state()

        # Sincronizar niveles configurados vs state persistido
        for lvl in LEVELS:
            key = str(lvl)
            if key not in self.state["levels"]:
                self.state["levels"][key] = _empty_level()
        _save_state(self.state)

    # ── Propiedades runtime ───────────────────────────────────────────────────

    @property
    def paused(self) -> bool:
        return bool(self.state.get("paused"))

    @property
    def grid_low(self) -> float:
        return float(self.state.get("grid_low", GRID_LOW))

    @property
    def grid_high(self) -> float:
        return float(self.state.get("grid_high", GRID_HIGH))

    # ── Startup: reconciliación ───────────────────────────────────────────────

    def _committed_capital(self) -> float:
        """Capital total comprometido en órdenes y posiciones abiertas."""
        return sum(
            CAPITAL_USDC for lvl in self.state["levels"].values()
            if lvl["status"] != IDLE
        )

    def reconcile(self, current_price: float) -> dict:
        """Compara state vs órdenes reales en Hyperliquid; coloca las faltantes."""
        with self._lock:
            open_oids = {o["oid"] for o in self.client.get_open_orders()}
            placed    = []
            restored  = []
            errors    = []
            skipped   = []

            # Capital ya comprometido antes de empezar a colocar nuevas órdenes
            committed = self._committed_capital()

            for level_str, lvl in self.state["levels"].items():
                level  = float(level_str)
                status = lvl["status"]

                if level >= current_price:
                    # Por encima del precio: no colocar compras
                    if status == WAITING_BUY:
                        oid = lvl.get("buy_order_id")
                        if oid and oid not in open_oids:
                            lvl.update({"status": IDLE, "buy_order_id": None})
                    continue

                if status == IDLE:
                    if committed + CAPITAL_USDC > MAX_CAPITAL_USDC:
                        skipped.append(level)
                        logger.info("Cap alcanzado (%.0f/%.0f) — saltando nivel %.2f",
                                    committed, MAX_CAPITAL_USDC, level)
                    elif self._place_buy(level_str, level):
                        committed += CAPITAL_USDC
                        placed.append(level)
                    else:
                        errors.append(level)

                elif status == WAITING_BUY:
                    oid = lvl.get("buy_order_id")
                    if not oid or oid not in open_oids:
                        lvl.update({"status": IDLE, "buy_order_id": None})
                        if committed + CAPITAL_USDC > MAX_CAPITAL_USDC:
                            skipped.append(level)
                        elif self._place_buy(level_str, level):
                            committed += CAPITAL_USDC
                            restored.append(level)
                        else:
                            errors.append(level)

                elif status == WAITING_SELL:
                    oid = lvl.get("sell_order_id")
                    if not oid or oid not in open_oids:
                        sell_px  = round(level + LEVEL_SPACING, 2)
                        qty      = lvl.get("qty") or round(CAPITAL_USDC / level, 4)
                        result   = self.client.limit_sell(ASSET, qty, sell_px)
                        if result.success:
                            lvl["sell_order_id"] = result.order_id
                            lvl["sell_price"]    = sell_px
                            restored.append(level)
                            logger.info("Sell restaurado | nivel=%.2f px=%.4f", level, sell_px)
                        else:
                            errors.append(level)

            _save_state(self.state)
            return {"placed": placed, "restored": restored,
                    "errors": errors, "skipped": skipped}

    def _place_buy(self, level_str: str, level: float) -> bool:
        qty    = round(CAPITAL_USDC / level, 4)
        result = self.client.limit_buy(ASSET, qty, level)
        if result.success:
            lvl = self.state["levels"][level_str]
            lvl.update({
                "status":        WAITING_BUY,
                "buy_order_id":  result.order_id,
                "expected_qty":  qty,
                "buy_price":     None,
                "sell_price":    None,
                "qty":           0.0,
            })
            logger.info("Buy colocado | nivel=%.2f qty=%.4f oid=%s", level, qty, result.order_id)
            return True
        logger.error("Error colocando buy en %.2f | status=%s | raw=%s",
                     level, result.status, result.raw)
        return False

    # ── Fill handler ──────────────────────────────────────────────────────────

    def on_fill(self, fill: dict):
        oid  = fill.get("oid")
        side = fill.get("side", "")
        px   = float(fill.get("px", 0))
        sz   = float(fill.get("sz", 0))
        logger.info("Fill | side=%s oid=%s px=%.4f sz=%.4f", side, oid, px, sz)
        with self._lock:
            if side == "B":
                self._on_buy_filled(oid, px, sz)
            elif side == "A":
                self._on_sell_filled(oid, px, sz)

    def _on_buy_filled(self, oid: int, px: float, sz: float):
        level_str = self._find_by_oid(oid, "buy")
        if not level_str:
            logger.debug("Fill compra oid=%s sin nivel asociado", oid)
            return

        lvl = self.state["levels"][level_str]
        if lvl["buy_price"] is None:
            lvl["buy_price"] = px
        lvl["qty"] += sz

        expected = lvl.get("expected_qty", 0)
        if expected > 0 and lvl["qty"] < expected * 0.99:
            _save_state(self.state)
            logger.info("Fill parcial buy | nivel=%s qty=%.4f/%.4f", level_str, lvl["qty"], expected)
            return

        # Fill completo → colocar venta en el nivel siguiente
        lvl["buy_order_id"] = None
        sell_px = round(float(level_str) + LEVEL_SPACING, 2)
        qty     = lvl["qty"]
        result  = self.client.limit_sell(ASSET, qty, sell_px)

        if result.success:
            lvl.update({
                "status":        WAITING_SELL,
                "sell_order_id": result.order_id,
                "sell_price":    sell_px,
            })
            logger.info("Sell colocado | nivel=%s px=%.4f qty=%.4f oid=%s",
                        level_str, sell_px, qty, result.order_id)
        else:
            lvl["status"] = WAITING_SELL   # sin sell_order_id; reconcile lo repondrá
            logger.error("No se pudo colocar sell en nivel=%s: %s", level_str, result.status)

        _save_state(self.state)
        self.notifier.alert_grid_buy(
            level=float(level_str), price=px, qty=qty, sell_price=sell_px,
        )

    def _on_sell_filled(self, oid: int, px: float, sz: float):
        level_str = self._find_by_oid(oid, "sell")
        if not level_str:
            logger.debug("Fill venta oid=%s sin nivel asociado", oid)
            return

        lvl       = self.state["levels"][level_str]
        buy_price = lvl.get("buy_price") or float(level_str)
        qty       = lvl.get("qty") or sz
        level     = float(level_str)

        gross    = (px - buy_price) * qty
        buy_fee  = qty * buy_price * MAKER_FEE
        sell_fee = qty * px * MAKER_FEE
        pnl_net  = gross - buy_fee - sell_fee

        self.state["completed_cycles"].append({
            "level":        level,
            "buy_price":    buy_price,
            "sell_price":   px,
            "qty":          qty,
            "pnl_net":      round(pnl_net, 4),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
        self.state["total_realized_pnl"] = round(
            self.state["total_realized_pnl"] + pnl_net, 4
        )

        # Reset nivel + nuevo buy
        self.state["levels"][level_str] = _empty_level()
        if not self.paused:
            self._place_buy(level_str, level)

        _save_state(self.state)
        self.notifier.alert_grid_sell(
            level=level, buy_price=buy_price, sell_price=px,
            qty=qty, pnl_net=pnl_net,
            total_pnl=self.state["total_realized_pnl"],
        )

    # ── Detección de rango (desde allMids WebSocket) ──────────────────────────

    def on_price(self, price: float):
        """Llamado en cada tick de precio. Detecta salida/retorno al rango."""
        in_range = self.grid_low <= price <= self.grid_high

        if not in_range and not self.paused:
            self._pause(price)
        elif in_range and self.paused and self.state.get("pause_reason") == "out_of_range":
            self._auto_reactivate(price)

    def _pause(self, price: float):
        with self._lock:
            if self.paused:
                return
            direction = "inferior" if price < self.grid_low else "superior"
            self.state["paused"]       = True
            self.state["pause_reason"] = "out_of_range"
            _save_state(self.state)

        hype_inv     = self.hype_in_inventory()
        latent_loss  = self._latent_pnl(price) if price < self.grid_low else None
        usdc_avail   = self.client.get_usdc_balance() if price > self.grid_high else None

        logger.warning("Precio $%.4f fuera de rango [%.2f-%.2f] — bot pausado",
                       price, self.grid_low, self.grid_high)
        self.notifier.alert_grid_out_of_range(
            direction=direction,
            price=price,
            grid_low=self.grid_low,
            grid_high=self.grid_high,
            hype_inventory=hype_inv,
            latent_loss=latent_loss,
            usdc_available=usdc_avail,
        )

    def _auto_reactivate(self, price: float):
        with self._lock:
            self.state["paused"]       = False
            self.state["pause_reason"] = None
            _save_state(self.state)
        logger.info("Precio volvió al rango ($%.4f) — reactivando", price)
        self.notifier.alert_grid_reactivated(price)
        self.reconcile(price)

    # ── Comandos: /reactivar, /shift_down, /shift_up ─────────────────────────

    def reactivar(self, current_price: float) -> dict:
        with self._lock:
            self.state["paused"]       = False
            self.state["pause_reason"] = None
            _save_state(self.state)
        result = self.reconcile(current_price)
        self.notifier.alert_grid_reactivated(current_price)
        return result

    def shift(self, direction: str, current_price: float) -> dict:
        """
        Cancela todas las órdenes abiertas, recalcula la grilla centrada en
        el precio actual (N_SHIFT_LEVELS niveles), recoloca compras.
        """
        # 1. Cancelar todas las órdenes abiertas de HYPE
        cancelled = self.client.cancel_all_orders(ASSET)
        logger.info("Shift %s: canceladas %d órdenes", direction, len(cancelled))

        # 2. Calcular nuevo rango centrado en precio actual
        half       = (N_SHIFT_LEVELS // 2) * LEVEL_SPACING
        new_low    = round(current_price - half, 2)
        new_high   = round(new_low + (N_SHIFT_LEVELS - 1) * LEVEL_SPACING, 2)
        new_levels_list = [round(new_low + i * LEVEL_SPACING, 2) for i in range(N_SHIFT_LEVELS)]

        with self._lock:
            old_levels = self.state["levels"]

            # Preservar WAITING_SELL (HYPE ya comprado, venta pendiente)
            new_levels_dict = {}
            for lvl in new_levels_list:
                key = str(lvl)
                if key in old_levels and old_levels[key]["status"] == WAITING_SELL:
                    new_levels_dict[key] = old_levels[key]
                else:
                    new_levels_dict[key] = _empty_level()

            self.state.update({
                "grid_low":    new_low,
                "grid_high":   new_high,
                "levels":      new_levels_dict,
                "paused":      False,
                "pause_reason": None,
            })
            _save_state(self.state)

        # 3. Colocar órdenes en el nuevo rango
        result = self.reconcile(current_price)

        self.notifier.alert_grid_shifted(
            direction=direction,
            new_low=new_low,
            new_high=new_high,
            price=current_price,
            placed=result["placed"],
        )
        return {"new_low": new_low, "new_high": new_high, **result}

    # ── Estadísticas ──────────────────────────────────────────────────────────

    def stats_24h(self) -> dict:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        recent = [
            c for c in self.state["completed_cycles"]
            if datetime.fromisoformat(c["completed_at"]) >= cutoff
        ]
        return {
            "cycles": len(recent),
            "profit": round(sum(c["pnl_net"] for c in recent), 4),
        }

    def hype_in_inventory(self) -> float:
        return round(sum(
            lvl["qty"]
            for lvl in self.state["levels"].values()
            if lvl["status"] == WAITING_SELL
        ), 4)

    def _latent_pnl(self, current_price: float) -> float:
        total = 0.0
        for lvl in self.state["levels"].values():
            if lvl["status"] == WAITING_SELL and lvl.get("buy_price"):
                qty   = lvl["qty"]
                entry = lvl["buy_price"]
                fee   = qty * entry * MAKER_FEE
                total += qty * (current_price - entry) - fee
        return round(total, 2)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _find_by_oid(self, oid: int, side: str) -> Optional[str]:
        key = "buy_order_id" if side == "buy" else "sell_order_id"
        for level_str, lvl in self.state["levels"].items():
            if lvl.get(key) == oid:
                return level_str
        return None
