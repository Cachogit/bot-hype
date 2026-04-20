# -*- coding: utf-8 -*-
"""
Estrategia en vivo: zonas de soporte HYPE con RSI.
Gestiona estado de paper positions persistido en JSON.
"""
import json
import logging
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from hyperliquid.info import Info
from hyperliquid.utils import constants

logger = logging.getLogger(__name__)

STATE_FILE    = Path(__file__).parent.parent.parent / "data" / "paper_state.json"
STATE_FILE_15M = Path(__file__).parent.parent.parent / "data" / "paper_state_15m.json"

ZONES_CFG = [
    dict(level=1, low=42.98, high=43.50, rsi_entry=40, capital=2000.0),
    dict(level=2, low=40.22, high=40.60, rsi_entry=40, capital=3000.0),
    dict(level=3, low=37.80, high=38.35, rsi_entry=30, capital=4000.0),
    dict(level=4, low=45.50, high=46.20, rsi_entry=40, capital=1500.0),
]

TP1_PCT        = 0.025
TP2_PCT        = 0.045
REENTRY_TP_PCT = 0.013
TAKER_FEE      = 0.0005
MAKER_FEE      = 0.0001
RSI_PERIOD     = 14
CANDLE_BUFFER  = 50   # velas a descargar para calcular RSI estable


# ── MODELOS DE ESTADO ──────────────────────────────────────────────────────────

@dataclass
class PaperPosition:
    id:           str
    zone:         int
    reentry:      bool
    entry_time:   str      # ISO
    entry_price:  float
    qty:          float
    capital:      float
    tp1:          float
    tp2:          float
    entry_fee:    float
    tp1_hit:      bool  = False
    tp2_hit:      bool  = False
    exit1_time:   str   = ""
    exit2_time:   str   = ""
    gross_pnl:    float = 0.0
    fees:         float = 0.0

    @property
    def closed(self) -> bool:
        return self.tp1_hit if self.reentry else self.tp2_hit

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fees

    @property
    def tag(self) -> str:
        return "RE-ENTRY" if self.reentry else "INICIAL"


@dataclass
class BotState:
    positions:     list = field(default_factory=list)   # list of dicts
    total_net_pnl: float = 0.0
    last_run:      str = ""
    run_count:     int = 0


# ── PERSISTENCIA ──────────────────────────────────────────────────────────────

def load_state(path: Path = STATE_FILE) -> BotState:
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return BotState(**raw)
        except Exception as e:
            logger.warning("No se pudo cargar estado: %s — empezando limpio", e)
    return BotState()


def save_state(state: BotState, path: Path = STATE_FILE):
    path.write_text(
        json.dumps(asdict(state) if hasattr(state, "__dataclass_fields__")
                   else state.__dict__, indent=2, default=str),
        encoding="utf-8",
    )


def positions_from_state(state: BotState) -> list[PaperPosition]:
    return [PaperPosition(**p) for p in state.positions]


def positions_to_state(state: BotState, positions: list[PaperPosition]):
    state.positions = [asdict(p) for p in positions]


# ── DATOS DE MERCADO ───────────────────────────────────────────────────────────

_TF_MS = {"1h": 3600_000, "15m": 900_000}


def fetch_candles(n: int = CANDLE_BUFFER, timeframe: str = "1h") -> pd.DataFrame:
    info     = Info(base_url=constants.MAINNET_API_URL, skip_ws=True)
    end_ms   = int(time.time() * 1000)
    start_ms = end_ms - n * _TF_MS[timeframe]

    raw = info.candles_snapshot("HYPE", timeframe, start_ms, end_ms)
    if not raw:
        raise RuntimeError("No se recibieron velas de Hyperliquid")

    df = pd.DataFrame(raw)
    df = df.rename(columns={"t": "ts", "o": "open", "h": "high",
                             "l": "low",  "c": "close", "v": "volume"})
    df["ts"]    = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df          = df.set_index("ts").sort_index()
    df[["open","high","low","close","volume"]] = (
        df[["open","high","low","close","volume"]].astype(float)
    )
    return df


def compute_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    delta    = closes.diff()
    up       = delta.clip(lower=0)
    down     = -delta.clip(upper=0)
    avg_gain = up.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = down.ewm(com=period - 1, min_periods=period).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


# ── LOGICA DE ESTRATEGIA ───────────────────────────────────────────────────────

class LiveZoneStrategy:

    def __init__(self, timeframe: str = "1h"):
        self.timeframe  = timeframe
        state_file = STATE_FILE_15M if timeframe == "15m" else STATE_FILE
        self.state     = load_state(state_file)
        self.positions = positions_from_state(self.state)
        self.zones     = ZONES_CFG
        self._state_file = state_file

    def run_cycle(self) -> dict:
        """
        Ejecuta un ciclo completo:
        1. Descarga las ultimas N velas 1H
        2. Calcula RSI
        3. Verifica TPs sobre posiciones abiertas
        4. Detecta nuevas senales de entrada
        Retorna un dict con los eventos del ciclo para que el monitor los procese.
        """
        now = datetime.now(timezone.utc).isoformat()
        logger.info("=== Ciclo %d [%s] | %s ===", self.state.run_count + 1, self.timeframe, now)

        df = fetch_candles(CANDLE_BUFFER, self.timeframe)
        df["rsi"] = compute_rsi(df["close"], RSI_PERIOD)

        # usamos las 2 ultimas velas cerradas (la ultima puede estar en curso)
        prev = df.iloc[-2]
        curr = df.iloc[-1]
        prev_rsi  = df["rsi"].iloc[-2]
        curr_rsi  = df["rsi"].iloc[-1]
        curr_time = df.index[-1]
        c_high    = curr["high"]
        c_low     = curr["low"]
        c_close   = curr["close"]

        events = {
            "timestamp":    now,
            "timeframe":    self.timeframe,
            "price":        c_close,
            "rsi":          curr_rsi,
            "tp_hits":      [],
            "entries":      [],
            "zones_status": [],
        }

        # ── 1. Verificar TPs ───────────────────────────────────────────────
        open_pos = [p for p in self.positions if not p.closed]
        for pos in open_pos:
            if pos.reentry:
                if not pos.tp1_hit and c_high >= pos.tp1:
                    exit_fee    = pos.qty * pos.tp1 * MAKER_FEE
                    pos.fees   += exit_fee
                    pos.gross_pnl = pos.qty * (pos.tp1 - pos.entry_price)
                    pos.tp1_hit  = True
                    pos.exit1_time = curr_time.isoformat()
                    events["tp_hits"].append({
                        "pos": pos, "tp_num": 1,
                        "qty": pos.qty, "tp_price": pos.tp1,
                        "pnl": pos.net_pnl, "fees": pos.fees,
                    })
                    logger.info("TP hit Z%d re-entry | pnl=$%.2f", pos.zone, pos.net_pnl)
            else:
                if not pos.tp1_hit and c_high >= pos.tp1:
                    exit_fee    = (pos.qty / 2) * pos.tp1 * MAKER_FEE
                    pos.fees   += exit_fee
                    pos.gross_pnl += (pos.qty / 2) * (pos.tp1 - pos.entry_price)
                    pos.tp1_hit  = True
                    pos.exit1_time = curr_time.isoformat()
                    partial_pnl = (pos.qty / 2) * (pos.tp1 - pos.entry_price) - pos.entry_fee - exit_fee
                    events["tp_hits"].append({
                        "pos": pos, "tp_num": 1,
                        "qty": pos.qty / 2, "tp_price": pos.tp1,
                        "pnl": partial_pnl, "fees": exit_fee,
                    })
                    logger.info("TP1 hit Z%d | pnl parcial=$%.2f", pos.zone, partial_pnl)

                if pos.tp1_hit and not pos.tp2_hit and c_high >= pos.tp2:
                    exit_fee    = (pos.qty / 2) * pos.tp2 * MAKER_FEE
                    pos.fees   += exit_fee
                    pos.gross_pnl += (pos.qty / 2) * (pos.tp2 - pos.entry_price)
                    pos.tp2_hit  = True
                    pos.exit2_time = curr_time.isoformat()
                    partial_pnl = (pos.qty / 2) * (pos.tp2 - pos.entry_price) - exit_fee
                    events["tp_hits"].append({
                        "pos": pos, "tp_num": 2,
                        "qty": pos.qty / 2, "tp_price": pos.tp2,
                        "pnl": partial_pnl, "fees": exit_fee,
                    })
                    logger.info("TP2 hit Z%d | pnl=$%.2f", pos.zone, partial_pnl)

        # ── 2. Detectar nuevas entradas ────────────────────────────────────
        rsi_valid = not (np.isnan(prev_rsi) or np.isnan(curr_rsi))

        for cfg in self.zones:
            lvl, z_low, z_high = cfg["level"], cfg["low"], cfg["high"]
            threshold = cfg["rsi_entry"]
            capital   = cfg["capital"]

            in_zone   = (c_low <= z_high) and (c_close >= z_low * 0.98)
            rsi_cross = rsi_valid and (prev_rsi < threshold <= curr_rsi)

            events["zones_status"].append({
                **cfg,
                "in_zone": in_zone,
                "curr_rsi": round(curr_rsi, 1) if not np.isnan(curr_rsi) else 0,
            })

            if rsi_cross and in_zone:
                still_open  = [p for p in self.positions
                               if not p.closed and p.zone == lvl]
                is_reentry  = len(still_open) > 0
                entry_price = c_close
                qty         = capital / entry_price
                entry_fee   = qty * entry_price * TAKER_FEE

                tf_tag = "15m" if self.timeframe == "15m" else "1h"
                pos_id = f"Z{lvl}-{tf_tag}-{curr_time.strftime('%Y%m%d%H%M')}"

                if is_reentry:
                    tp1 = entry_price * (1 + REENTRY_TP_PCT)
                    tp2 = 0.0
                else:
                    tp1 = entry_price * (1 + TP1_PCT)
                    tp2 = entry_price * (1 + TP2_PCT)

                pos = PaperPosition(
                    id=pos_id, zone=lvl, reentry=is_reentry,
                    entry_time=curr_time.isoformat(),
                    entry_price=entry_price, qty=qty,
                    capital=capital, tp1=tp1, tp2=tp2,
                    entry_fee=entry_fee, fees=entry_fee,
                )
                self.positions.append(pos)

                events["entries"].append({
                    "pos": pos, "rsi": curr_rsi,
                    "is_reentry": is_reentry,
                })
                logger.info(
                    "%s Z%d | px=%.4f rsi=%.1f tp1=%.4f tp2=%.4f",
                    pos.tag, lvl, entry_price, curr_rsi, tp1, tp2,
                )

        # ── 3. Guardar estado ──────────────────────────────────────────────
        closed_pnl = sum(p.net_pnl for p in self.positions if p.closed)
        self.state.total_net_pnl = closed_pnl
        self.state.last_run      = now
        self.state.run_count    += 1
        positions_to_state(self.state, self.positions)
        save_state(self.state, self._state_file)

        return events

    def summary(self, last_price: float) -> str:
        """Texto de resumen de posiciones abiertas para logging."""
        open_pos = [p for p in self.positions if not p.closed]
        lines    = [f"Posiciones abiertas: {len(open_pos)}"]
        for p in open_pos:
            unreal = p.qty * (last_price - p.entry_price) - p.entry_fee
            if not p.reentry and p.tp1_hit:
                unreal = (p.qty/2) * (last_price - p.entry_price)
            lines.append(
                f"  Z{p.zone} {p.tag} px={p.entry_price:.4f} "
                f"unreal=${unreal:+.2f} tp1={'OK' if p.tp1_hit else 'pend.'}"
            )
        lines.append(f"PnL cerrado total: ${self.state.total_net_pnl:+.2f}")
        return "\n".join(lines)
