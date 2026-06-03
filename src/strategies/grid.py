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
    LEVEL_SPACING_PCT, SZ_DECIMALS, PRICE_DECIMALS, MAX_AUTO_SHIFTS, N_LEVELS,
    calc_levels,
)

N_SHIFT_LEVELS = N_LEVELS

logger = logging.getLogger(__name__)

STATE_FILE       = Path(__file__).parent.parent.parent / "data" / "grid_state.json"
PNL_HISTORY_FILE = Path(__file__).parent.parent.parent / "data" / "pnl_history.json"

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
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("grid_state.json inválido: %s — empezando limpio", e)
    return {
        "grid_low":           0.0,
        "grid_high":          0.0,
        "levels":             {},
        "completed_cycles":   [],
        "total_realized_pnl": 0.0,
        "paused":             False,
        "pause_reason":       None,
        "detenido":           False,
        "auto_shift_count":   0,
        "capital_per_level":  CAPITAL_USDC,
        "esperando_entrada":  False,
    }


def _save_state(state: dict):
    STATE_FILE.write_text(
        json.dumps(state, indent=2, default=str),
        encoding="utf-8",
    )


def _append_pnl_history(entry: dict):
    try:
        if PNL_HISTORY_FILE.exists():
            history = json.loads(PNL_HISTORY_FILE.read_text(encoding="utf-8"))
        else:
            history = []
        history.append(entry)
        PNL_HISTORY_FILE.write_text(
            json.dumps(history, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception as e:
        logger.error("Error guardando pnl_history: %s", e)


# ─────────────────────────────────────────────────────────────────────────────


class GridStrategy:

    def __init__(self, client, notifier):
        self.client   = client
        self.notifier = notifier
        self._lock    = threading.Lock()
        self.state    = _load_state()
        if "capital_per_level" not in self.state:
            self.state["capital_per_level"] = CAPITAL_USDC
        self._above_range_alerted  = False
        self._last_shift_price     = 0.0
        self._rebalance_callback   = None  # se asigna desde grid_monitor

    # ── Propiedades runtime ───────────────────────────────────────────────────

    @property
    def paused(self) -> bool:
        return bool(self.state.get("paused"))

    @property
    def detenido(self) -> bool:
        return bool(self.state.get("detenido"))

    @property
    def esperando_entrada(self) -> bool:
        return bool(self.state.get("esperando_entrada"))

    @property
    def grid_low(self) -> float:
        return float(self.state.get("grid_low", 0.0))

    @property
    def grid_high(self) -> float:
        return float(self.state.get("grid_high", 0.0))

    # ── Startup: reconciliación ───────────────────────────────────────────────

    def _committed_capital(self) -> float:
        cap = self.state["capital_per_level"]
        return sum(
            cap for lvl in self.state["levels"].values()
            if lvl["status"] == WAITING_BUY
        )

    def reconcile(self, current_price: float) -> dict:
        """Compara state vs órdenes reales en Hyperliquid; coloca las faltantes."""
        if not self.state.get("levels"):
            new_levels = calc_levels(current_price)
            self.state["levels"]    = {str(lvl): _empty_level() for lvl in new_levels}
            self.state["grid_low"]  = new_levels[0]
            self.state["grid_high"] = new_levels[-1]
            _save_state(self.state)
            logger.info("Grilla inicializada desde $%.4f | rango [%.4f - %.4f]",
                        current_price, new_levels[0], new_levels[-1])

        with self._lock:
            cancelled = self.client.cancel_all_orders(ASSET, side="B")
            if cancelled:
                logger.info("reconcile: canceladas %d órdenes de compra previas", len(cancelled))
            open_orders_list = self.client.get_open_orders()
            open_oids        = {o["oid"] for o in open_orders_list}
            open_sell_oids   = {o["oid"] for o in open_orders_list if o.get("side") == "A"}
            placed    = []
            restored  = []
            errors    = []
            skipped   = []

            try:
                recent_fills   = self.client.get_recent_fills()
                filled_buy_oids = {
                    f["oid"]: f for f in recent_fills if f.get("side") == "B"
                }
            except Exception as _fe:
                logger.warning("reconcile: no se pudieron obtener fills recientes: %s", _fe)
                filled_buy_oids = {}

            # Tras cancelar todas las compras, el USDC comprometido son solo
            # los niveles WAITING_SELL (su capital ya está convertido en HYPE).
            cap = self.state["capital_per_level"]
            committed = sum(
                cap for lvl in self.state["levels"].values()
                if lvl["status"] == WAITING_SELL
            )

            for level_str, lvl in self.state["levels"].items():
                level  = float(level_str)
                status = lvl["status"]

                # Recuperar fills que el WS no capturó: si la orden de compra ya no
                # está en open_orders pero sí aparece en fills REST, el HYPE está
                # en cuenta — pasar directo a WAITING_SELL para colocar la venta.
                if status == WAITING_BUY:
                    oid = lvl.get("buy_order_id")
                    if oid and oid not in open_oids and oid in filled_buy_oids:
                        fill = filled_buy_oids[oid]
                        lvl["buy_price"]     = float(fill.get("px", level))
                        lvl["qty"]           = float(fill.get("sz", lvl.get("expected_qty", 0)))
                        lvl["buy_order_id"]  = None
                        lvl["sell_order_id"] = None
                        lvl["status"]        = WAITING_SELL
                        status               = WAITING_SELL
                        committed           += cap
                        logger.info("Fill recuperado vía REST | nivel=%s px=%.4f qty=%.4f",
                                    level_str, lvl["buy_price"], lvl["qty"])

                if level >= current_price and status != WAITING_SELL:
                    # Por encima del precio: no colocar compras
                    if status == WAITING_BUY:
                        oid = lvl.get("buy_order_id")
                        if oid and oid not in open_oids:
                            lvl.update({"status": IDLE, "buy_order_id": None})
                    continue

                max_cap = cap * N_LEVELS

                if status == IDLE:
                    if committed + cap > max_cap:
                        skipped.append(level)
                        logger.info("Cap alcanzado (%.0f/%.0f) — saltando nivel %.2f",
                                    committed, max_cap, level)
                    elif self._place_buy(level_str, level):
                        committed += cap
                        placed.append(level)
                    else:
                        errors.append(level)

                elif status == WAITING_BUY:
                    oid = lvl.get("buy_order_id")
                    if not oid or oid not in open_oids:
                        lvl.update({"status": IDLE, "buy_order_id": None})
                        if committed + cap > max_cap:
                            skipped.append(level)
                        elif self._place_buy(level_str, level):
                            committed += cap
                            restored.append(level)
                        else:
                            errors.append(level)

                elif status == WAITING_SELL:
                    oid = lvl.get("sell_order_id")
                    logger.info("WAITING_SELL nivel=%.2f | oid=%s | en_exchange=%s",
                                level, oid, bool(oid and oid in open_sell_oids))
                    if not oid or oid not in open_sell_oids:
                        buy_ref = lvl.get("buy_price") or level
                        sell_px = round(buy_ref * (1 + LEVEL_SPACING_PCT), PRICE_DECIMALS)
                        # Si el mercado ya superó el precio de venta calculado,
                        # ajustar ligeramente por encima para que la ALO sea válida
                        if sell_px <= current_price:
                            sell_px = round(current_price * 1.001, PRICE_DECIMALS)
                            logger.warning("Sell price ajustado sobre mercado: $%.4f | nivel=%.2f",
                                           sell_px, level)
                        qty     = lvl.get("qty") or round(cap / level, SZ_DECIMALS)

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

                        time.sleep(2)  # dar tiempo al exchange a reflejar holds previos
                        result = self.client.limit_sell(ASSET, qty, sell_px)
                        if result.success:
                            lvl["sell_order_id"] = result.order_id
                            lvl["sell_price"]    = sell_px
                            restored.append(level)
                            logger.info("Sell restaurado | nivel=%.2f px=%.4f qty=%.4f", level, sell_px, qty)
                        else:
                            logger.error("Sell NO restaurado | nivel=%.2f px=%.4f qty=%.4f | raw=%s",
                                         level, sell_px, qty, result.raw)
                            errors.append(level)

            _save_state(self.state)
            return {"placed": placed, "restored": restored,
                    "errors": errors, "skipped": skipped}

    def _place_buy(self, level_str: str, level: float) -> bool:
        qty    = round(self.state["capital_per_level"] / level, SZ_DECIMALS)
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

        # Fill completo
        if self.state.get("esperando_entrada"):
            self.state["esperando_entrada"] = False
            logger.info("Primera compra ejecutada — esperando_entrada limpiado, operación normal")

        lvl["buy_order_id"] = None
        sell_px = round(px * (1 + LEVEL_SPACING_PCT), PRICE_DECIMALS)
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

        cycle = {
            "timestamp":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "level":      level,
            "buy_price":  buy_price,
            "sell_price": round(px, 4),
            "qty":        qty,
            "pnl_net":    round(pnl_net, 4),
        }
        self.state["completed_cycles"].append({**cycle, "completed_at": cycle["timestamp"]})
        _append_pnl_history(cycle)
        self.state["total_realized_pnl"] = round(
            self.state["total_realized_pnl"] + pnl_net, 4
        )

        # Reset nivel + nuevo buy
        self.state["levels"][level_str] = _empty_level()
        if not self.paused and not self.detenido:
            self._place_buy(level_str, level)

        # Resetear contador siempre; el flag solo cuando no quedan más ventas pendientes
        self.state["auto_shift_count"] = 0
        still_selling = any(
            lvl["status"] == WAITING_SELL
            for k, lvl in self.state["levels"].items()
            if k != level_str
        )
        if not still_selling:
            self._above_range_alerted = False
            if self._rebalance_callback and not self.detenido and not self.esperando_entrada:
                try:
                    self._rebalance_callback()
                except Exception as e:
                    logger.error("Error en rebalance callback: %s", e)

        _save_state(self.state)
        self.notifier.alert_grid_sell(
            level=level, buy_price=buy_price, sell_price=px,
            qty=qty, pnl_net=pnl_net,
            total_pnl=self.state["total_realized_pnl"],
        )

    # ── Detección de rango (desde allMids WebSocket) ──────────────────────────

    def on_price(self, price: float):
        """Llamado en cada tick de precio."""
        if self.detenido:
            return
        if self.esperando_entrada:
            return
        if price < self.grid_low:
            # Por debajo del piso: pausa automática
            if not self.paused:
                self._pause(price)
            self._above_range_alerted = False
        elif price > self.grid_high:
            # Por encima del techo: shift automático hacia arriba
            if self.paused and self.state.get("pause_reason") == "out_of_range":
                self._auto_reactivate(price)
            has_pending_sell = any(
                lvl["status"] == WAITING_SELL
                for lvl in self.state["levels"].values()
            )
            if has_pending_sell:
                self._above_range_alerted = True
                return
            if not self._above_range_alerted:
                # Shifts consecutivos: exigir que el precio suba al menos LEVEL_SPACING_PCT
                # desde el último shift antes de volver a shiftear
                if (self._last_shift_price > 0 and
                        price < self._last_shift_price * (1 + LEVEL_SPACING_PCT)):
                    return
                self._above_range_alerted = True
                logger.info("Precio $%.4f superó techo $%.2f — auto shift up", price, self.grid_high)
                result = self.shift("up", price, is_auto=True)
                if result:
                    self._last_shift_price = price
                    self._above_range_alerted = False  # permitir nuevo shift si el precio sigue subiendo
                else:
                    self._alert_above_range(price)
        else:
            # Dentro del rango: resetear para permitir shift libre la próxima vez
            self._above_range_alerted = False
            self._last_shift_price    = 0.0
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
            new_levels_list = calc_levels(current_price)
            new_low         = new_levels_list[0]
            new_high        = new_levels_list[-1]
            self.state["grid_low"]  = new_low
            self.state["grid_high"] = new_high
            self.state["levels"]    = {str(lvl): _empty_level() for lvl in new_levels_list}
            self.state["completed_cycles"]   = []
            self.state["total_realized_pnl"] = 0.0
            self.state["paused"]             = False
            self.state["pause_reason"]       = None
            self.state["detenido"]           = False
            self.state["auto_shift_count"]   = 0
            _save_state(self.state)
        logger.info("Grid reseteada — todos los niveles a IDLE, PnL en 0")
        return self.reconcile(current_price)

    def detener(self) -> dict:
        """Cancela todas las órdenes (buys y sells) y detiene el bot completamente."""
        with self._lock:
            cancelled_buys  = self.client.cancel_all_orders(ASSET, side="B")
            cancelled_sells = self.client.cancel_all_orders(ASSET, side="A")
            for lvl in self.state["levels"].values():
                if lvl["status"] in (WAITING_BUY, WAITING_SELL):
                    lvl.update({"status": IDLE, "buy_order_id": None, "sell_order_id": None, "qty": 0.0})
            self.state["detenido"]     = True
            self.state["paused"]       = True
            self.state["pause_reason"] = "detenido"
            _save_state(self.state)
        logger.info("Bot DETENIDO | buys cancelados=%d | sells cancelados=%d",
                    len(cancelled_buys), len(cancelled_sells))
        return {"cancelled_buys": len(cancelled_buys), "cancelled_sells": len(cancelled_sells)}

    def fijar(self, precio_techo: float, current_price: float) -> dict:
        """
        Fija la grilla con el techo en precio_techo (todas las órdenes por debajo).
        Activa esperando_entrada: bloquea auto-shift, auto-compound y on_price
        hasta que se ejecute la primera compra.
        """
        if precio_techo >= current_price:
            raise ValueError(
                f"El precio techo ${precio_techo:.4f} es mayor o igual al precio actual "
                f"${current_price:.4f}. Todas las órdenes deben quedar por debajo del mercado."
            )

        # 1. Cancelar todas las órdenes abiertas
        self.client.cancel_all_orders(ASSET, side="B")
        self.client.cancel_all_orders(ASSET, side="A")

        # 2. Calcular nueva grilla con el techo en precio_techo
        new_levels_list = calc_levels(precio_techo)
        new_low  = new_levels_list[0]
        new_high = new_levels_list[-1]

        with self._lock:
            self.state["levels"]            = {str(lvl): _empty_level() for lvl in new_levels_list}
            self.state["grid_low"]          = new_low
            self.state["grid_high"]         = new_high
            self.state["paused"]            = False
            self.state["pause_reason"]      = None
            self.state["detenido"]          = False
            self.state["auto_shift_count"]  = 0
            self.state["esperando_entrada"] = True
            _save_state(self.state)

        # 3. Colocar las 6 compras vía reconcile
        result = self.reconcile(current_price)

        cap = self.state["capital_per_level"]
        logger.info(
            "Grilla fijada | techo=$%.4f | rango=[%.4f-%.4f] | colocadas=%d | USDC=$%.0f",
            precio_techo, new_low, new_high, len(result["placed"]), cap * len(result["placed"]),
        )
        return {
            "precio_techo":      precio_techo,
            "grid_low":          new_low,
            "grid_high":         new_high,
            "placed":            result["placed"],
            "usdc_comprometido": cap * len(result["placed"]),
        }

    def pausar_manual(self, current_price: float):
        """Pausa manual desde Telegram. No se auto-reactiva; requiere /reactivar."""
        with self._lock:
            if self.paused:
                return
            cancelled = self.client.cancel_all_orders(ASSET, side="B")
            for lvl in self.state["levels"].values():
                if lvl["status"] == WAITING_BUY:
                    lvl.update({"status": IDLE, "buy_order_id": None})
            self.state["paused"]       = True
            self.state["pause_reason"] = "manual"
            _save_state(self.state)
        logger.info("Bot pausado manualmente | precio=$%.4f | órdenes canceladas=%d",
                    current_price, len(cancelled))
        self.notifier.alert_grid_paused_manual(price=current_price, cancelled=len(cancelled))

    def reactivar(self, current_price: float) -> dict:
        with self._lock:
            self.state["paused"]       = False
            self.state["pause_reason"] = None
            self.state["detenido"]     = False
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
        cancelled = self.client.cancel_all_orders(ASSET, side="B")
        logger.info("Shift %s: canceladas %d órdenes (auto=%s)", direction, len(cancelled), is_auto)

        # 2. Calcular nuevo rango porcentual desde el precio actual
        new_levels_list = calc_levels(current_price)
        new_low         = new_levels_list[0]
        new_high        = new_levels_list[-1]

        with self._lock:
            old_levels = self.state["levels"]

            # Nuevos niveles IDLE (o WAITING_SELL si coincide exactamente con el nuevo rango)
            new_levels_dict = {}
            for lvl in new_levels_list:
                key = str(lvl)
                if key in old_levels and old_levels[key]["status"] == WAITING_SELL:
                    new_levels_dict[key] = old_levels[key]
                else:
                    new_levels_dict[key] = _empty_level()

            # Preservar ventas pendientes que no caen en el nuevo rango para que el fill se registre
            for key, data in old_levels.items():
                if data["status"] == WAITING_SELL and key not in new_levels_dict:
                    new_levels_dict[key] = data

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

    def rebalance_capital(self, new_capital: float, current_price: float):
        """Actualiza capital por nivel y resetea la grilla."""
        old_capital = self.state["capital_per_level"]
        self.state["capital_per_level"] = new_capital
        logger.info("Rebalanceo capital: $%.0f → $%.0f", old_capital, new_capital)
        return self.reset_grid(current_price)

    def all_waiting_buy(self) -> bool:
        """True si ningún nivel tiene venta pendiente."""
        levels = self.state.get("levels", {})
        return bool(levels) and not any(
            lvl["status"] == WAITING_SELL for lvl in levels.values()
        )

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
