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
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from config.grid_config import (
    ASSET, CAPITAL_USDC, MAX_CAPITAL_USDC, MAKER_FEE,
    LEVEL_SPACING, LEVELS, GRID_LOW, GRID_HIGH, SZ_DECIMALS, MAX_AUTO_SHIFTS,
    N_LEVELS,
)

N_SHIFT_LEVELS = N_LEVELS

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).parent.parent.parent / "data" / "grid_state.json"

IDLE         = "IDLE"
WAITING_BUY  = "WAITING_BUY"
WAITING_SELL = "WAITING_SELL"


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
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            state["grid_low"]  = GRID_LOW
            state["grid_high"] = GRID_HIGH
            return state
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
        "auto_shift_count":   0,
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
        self._above_range_alerted = False

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
            cancelled = self.client.cancel_all_orders(ASSET, side="B")
            if cancelled:
                logger.info("reconcile: canceladas %d órdenes de compra previas", len(cancelled))
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
                        sell_px = round(level + LEVEL_SPACING, 2)
                        qty     = lvl.get("qty") or round(CAPITAL_USDC / level, SZ_DECIMALS)

                        try:
                            balances  = self.client.get_spot_balance(ASSET)
                            free_hype = balances[0].available if balances else 0.0
                        except Exception as e:
                            logger.error("No se pudo consultar saldo HYPE en reconcile: %s | nivel=%.2f", e, level)
                            errors.append(level)
                            continue

                        if free_hype <= 0:
                            logger.error("Saldo libre HYPE=0 — no se restaura sell | nivel=%.2f qty=%.4f", level, qty)
                            errors.append(level)
                            continue

                        if free_hype < qty:
                            logger.warning("Saldo libre HYPE %.4f < qty %.4f — ajustando sell en reconcile | nivel=%.2f",
                                           free_hype, qty, level)
                            qty = round(free_hype, SZ_DECIMALS)

                        result = self.client.limit_sell(ASSET, qty, sell_px)
                        if result.success:
                            lvl["sell_order_id"] = result.order_id
                            lvl["sell_price"]    = sell_px
                            restored.append(level)
                            logger.info("Sell restaurado | nivel=%.2f px=%.4f qty=%.4f", level, sell_px, qty)
                        else:
                            errors.append(level)

            _save_state(self.state)
            return {"placed": placed, "restored": restored,
                    "errors": errors, "skipped": skipped}

    def _place_buy(self, level_str: str, level: float) -> bool:
        qty    = round(CAPITAL_USDC / level, SZ_DECIMALS)
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

        result = None
        for attempt in range(1, 4):
            time.sleep(5)  # esperar que Hyperliquid acredite el saldo

            try:
                balances  = self.client.get_spot_balance(ASSET)
                free_hype = balances[0].available if balances else 0.0
            except Exception as e:
                logger.error("Intento %d/3 — no se pudo consultar saldo HYPE: %s | nivel=%s",
                             attempt, e, level_str)
                continue

            if free_hype <= 0:
                logger.warning("Intento %d/3 — saldo libre HYPE=0 | nivel=%s", attempt, level_str)
                continue

            sell_qty = qty
            if free_hype < qty:
                logger.warning("Intento %d/3 — saldo libre HYPE %.4f < qty %.4f — ajustando | nivel=%s",
                               attempt, free_hype, qty, level_str)
                sell_qty = round(free_hype * 0.999, SZ_DECIMALS)

            result = self.client.limit_sell(ASSET, sell_qty, sell_px)

            if result.success:
                qty = sell_qty
                lvl.update({
                    "status":        WAITING_SELL,
                    "sell_order_id": result.order_id,
                    "sell_price":    sell_px,
                })
                logger.info("Sell colocado (intento %d/3) | nivel=%s px=%.4f qty=%.4f oid=%s",
                            attempt, level_str, sell_px, qty, result.order_id)
                break

            # Salir del loop solo si el error no es por saldo insuficiente
            err = str(result.raw).lower()
            if "insufficient" in err and "balance" in err:
                logger.warning("Intento %d/3 — Insufficient balance, reintentando | nivel=%s",
                               attempt, level_str)
            else:
                logger.error("Intento %d/3 — error no recuperable: %s | nivel=%s",
                             attempt, result.status, level_str)
                break

        if result is None or not result.success:
            lvl["status"] = WAITING_SELL   # sin sell_order_id; reconcile lo repondrá
            logger.error("Sell fallido tras 3 intentos | nivel=%s", level_str)

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
        """Llamado en cada tick de precio."""
        if price < self.grid_low:
            # Por debajo del piso: pausa automática
            if not self.paused:
                self._pause(price)
            self._above_range_alerted = False
        elif price > self.grid_high:
            # Por encima del techo: solo alerta, bot sigue activo
            if self.paused and self.state.get("pause_reason") == "out_of_range":
                self._auto_reactivate(price)
            threshold = self.grid_high * 1.015
            if price > threshold and not self._above_range_alerted:
                self._alert_above_range(price)
                self._above_range_alerted = True
            elif price <= threshold:
                self._above_range_alerted = False
        else:
            # Dentro del rango
            self._above_range_alerted = False
            if self.paused and self.state.get("pause_reason") == "out_of_range":
                self._auto_reactivate(price)

    def _pause(self, price: float):
        with self._lock:
            if self.paused:
                return
            self.state["paused"]       = True
            self.state["pause_reason"] = "out_of_range"
            _save_state(self.state)

        hype_inv    = self.hype_in_inventory()
        latent_loss = self._latent_pnl(price)

        logger.warning("Precio $%.4f por debajo del piso %.2f — bot pausado",
                       price, self.grid_low)
        self.notifier.alert_grid_out_of_range(
            direction="inferior",
            price=price,
            grid_low=self.grid_low,
            grid_high=self.grid_high,
            hype_inventory=hype_inv,
            latent_loss=latent_loss,
            usdc_available=None,
        )

    def _alert_above_range(self, price: float):
        logger.info("Precio $%.4f supera techo+1.5%% (%.2f) — alerta enviada",
                    price, self.grid_high * 1.015)
        self.notifier.alert_grid_above_range(
            price=price,
            grid_high=self.grid_high,
        )

    def _auto_reactivate(self, price: float):
        with self._lock:
            self.state["paused"]       = False
            self.state["pause_reason"] = None
            _save_state(self.state)
        logger.info("Precio volvió al rango ($%.4f) — reactivando", price)
        self.notifier.alert_grid_reactivated(price)
        self.reconcile(price)

    # ── Comandos: /reactivar, /shift_down, /shift_up, /reset_grid ───────────

    def reset_grid(self, current_price: float) -> dict:
        """Limpia todo el estado y recoloca órdenes desde cero."""
        with self._lock:
            self.state["levels"]             = {str(lvl): _empty_level() for lvl in LEVELS}
            self.state["completed_cycles"]   = []
            self.state["total_realized_pnl"] = 0.0
            self.state["paused"]             = False
            self.state["pause_reason"]       = None
            self.state["auto_shift_count"]   = 0
            _save_state(self.state)
        logger.info("Grid reseteada — todos los niveles a IDLE, PnL en 0")
        return self.reconcile(current_price)

    def reactivar(self, current_price: float) -> dict:
        with self._lock:
            self.state["paused"]       = False
            self.state["pause_reason"] = None
            _save_state(self.state)
        result = self.reconcile(current_price)
        self.notifier.alert_grid_reactivated(current_price)
        return result

    def shift(self, direction: str, current_price: float, is_auto: bool = False) -> dict:
        """
        Cancela todas las órdenes abiertas, recalcula la grilla por debajo del
        precio actual (new_high = precio * 0.995), recoloca compras.

        is_auto=True: shift disparado automáticamente.  Se bloquea si el contador
        de shifts automáticos ya alcanzó MAX_AUTO_SHIFTS; el usuario debe usar
        /shift_up para resetear el contador y mover la grilla manualmente.
        is_auto=False (manual): siempre ejecuta y resetea el contador.
        """
        if is_auto:
            count = self.state.get("auto_shift_count", 0)
            if count >= MAX_AUTO_SHIFTS:
                logger.warning("Auto-shift bloqueado: %d/%d shifts automáticos alcanzados",
                               count, MAX_AUTO_SHIFTS)
                self.notifier.alert_auto_shift_blocked(
                    count=count, max_shifts=MAX_AUTO_SHIFTS, price=current_price,
                )
                return {}
            self.state["auto_shift_count"] = count + 1
        else:
            self.state["auto_shift_count"] = 0   # reset al hacer shift manual

        # 1. Cancelar todas las órdenes abiertas de HYPE
        cancelled = self.client.cancel_all_orders(ASSET)
        logger.info("Shift %s: canceladas %d órdenes (auto=%s)", direction, len(cancelled), is_auto)

        # 2. Calcular nuevo rango: high justo bajo el precio, low = high - rango completo
        new_high        = round(current_price - LEVEL_SPACING, 2)
        new_low         = round(new_high - (N_LEVELS - 1) * LEVEL_SPACING, 2)
        new_levels_list = [round(new_low + i * LEVEL_SPACING, 2) for i in range(N_LEVELS)]

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
