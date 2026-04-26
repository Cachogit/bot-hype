# -*- coding: utf-8 -*-
"""
Cliente de Hyperliquid para operaciones spot en subcuenta.

Uso:
    from src.exchanges.hyperliquid_client import HyperliquidClient
    client = HyperliquidClient.from_env()
    balance = client.get_spot_balance()
    order   = client.market_buy("HYPE", usdc_amount=500)
"""
import os
import logging
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.utils.signing import OrderType

load_dotenv()
logger = logging.getLogger(__name__)

MAINNET_URL = constants.MAINNET_API_URL
TESTNET_URL = constants.TESTNET_API_URL

# Slippage por defecto para ordenes de mercado (0.1%)
DEFAULT_SLIPPAGE = 0.001


@dataclass
class OrderResult:
    success:    bool
    order_id:   int | None
    status:     str
    filled_sz:  float
    avg_px:     float
    raw:        dict


@dataclass
class SpotBalance:
    coin:      str
    total:     float
    available: float
    held:      float    # en ordenes abiertas


class HyperliquidClient:
    """
    Wrapper sobre el SDK oficial de Hyperliquid para operar spot en subcuenta.

    La wallet principal (private_key) firma las transacciones.
    account_address indica la subcuenta desde la que se opera.
    """

    def __init__(
        self,
        private_key: str,
        subaccount_address: str,
        network: str = "testnet",
    ):
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key

        self.wallet            = Account.from_key(private_key)
        self.subaccount        = subaccount_address.lower()
        self.network           = network
        self.base_url          = MAINNET_URL if network == "mainnet" else TESTNET_URL

        # Info: consultas de mercado y cuenta (no requiere firma)
        self.info = Info(base_url=self.base_url, skip_ws=True)

        # Exchange: operaciones firmadas; account_address apunta a la subcuenta
        self.exchange = Exchange(
            wallet=self.wallet,
            base_url=self.base_url,
            account_address=self.subaccount,
        )

        logger.info(
            "HyperliquidClient listo | red=%s | wallet=%s | subcuenta=%s",
            network,
            self.wallet.address[:10] + "...",
            self.subaccount[:10] + "...",
        )

    # ── FACTORY ───────────────────────────────────────────────────────────────

    @classmethod
    def from_env(cls) -> "HyperliquidClient":
        """Construye el cliente leyendo variables del .env"""
        private_key = os.environ.get("HYPERLIQUID_PRIVATE_KEY", "")
        subaccount  = os.environ.get("HYPERLIQUID_SUBACCOUNT_ADDRESS", "")
        network     = os.environ.get("HYPERLIQUID_NETWORK", "testnet")

        if not private_key or private_key.startswith("0xTU_"):
            raise ValueError(
                "HYPERLIQUID_PRIVATE_KEY no configurada. "
                "Edita el archivo .env con tu clave privada."
            )
        if not subaccount or subaccount.startswith("0xDIRECCION"):
            raise ValueError(
                "HYPERLIQUID_SUBACCOUNT_ADDRESS no configurada. "
                "Edita el archivo .env con la direccion de tu subcuenta."
            )

        return cls(private_key, subaccount, network)

    # ── CONSULTAS DE CUENTA ───────────────────────────────────────────────────

    def get_spot_balance(self, coin: str | None = None) -> list[SpotBalance]:
        """
        Retorna los balances spot de la subcuenta.
        Si coin != None, filtra por ese token (ej. 'HYPE', 'USDC').

        En Hyperliquid, USDC puede aparecer como coin='USDC' o coin='@0'
        (token index 0). El filtro acepta ambas representaciones.
        """
        state = self.info.spot_user_state(self.subaccount)
        logger.debug("spot_user_state raw: %s", state)
        result = []

        for entry in state.get("balances", []):
            c         = entry.get("coin", "")
            token_idx = entry.get("token")
            # Normalizar: "@0" y token==0 son USDC
            coin_name = "USDC" if (c == "@0" or token_idx == 0) else c

            if coin and coin_name.upper() != coin.upper():
                continue

            total = float(entry.get("total", 0))
            hold  = float(entry.get("hold",  0))
            result.append(SpotBalance(
                coin=coin_name,
                total=total,
                available=total - hold,
                held=hold,
            ))

        if coin and not result:
            result.append(SpotBalance(coin=coin.upper(), total=0.0, available=0.0, held=0.0))

        return result

    def get_usdc_balance(self) -> float:
        """Retorna el USDC disponible en la subcuenta spot."""
        balances = self.get_spot_balance("USDC")
        return balances[0].total if balances else 0.0

    def get_coin_balance(self, coin: str) -> float:
        """Retorna el balance total de un token spot."""
        balances = self.get_spot_balance(coin)
        return balances[0].total if balances else 0.0

    def get_open_orders(self) -> list[dict]:
        """Ordenes abiertas en la subcuenta."""
        return self.info.open_orders(self.subaccount)

    def get_recent_fills(self) -> list[dict]:
        """Ultimas operaciones ejecutadas en la subcuenta."""
        return self.info.user_fills(self.subaccount)

    # ── PRECIOS DE MERCADO ────────────────────────────────────────────────────

    def get_mid_price(self, coin: str) -> float:
        """Precio medio actual del par spot coin/USDC."""
        mids = self.info.all_mids()
        # en spot el nombre es "@N" internamente; all_mids incluye el symbol directo
        key = coin.upper()
        if key not in mids:
            # buscar en spot_meta por nombre
            meta = self.info.spot_meta()
            for token in meta.get("tokens", []):
                if token["name"].upper() == key:
                    idx = token["index"]
                    spot_key = f"@{idx}"
                    if spot_key in mids:
                        return float(mids[spot_key])
            raise ValueError(f"No se encontro precio para {coin}")
        return float(mids[key])

    def get_spot_meta(self) -> dict:
        """Metadata de todos los pares spot disponibles."""
        return self.info.spot_meta()

    # ── ORDENES DE MERCADO ────────────────────────────────────────────────────

    def market_buy(self, coin: str, usdc_amount: float) -> OrderResult:
        """
        Compra spot a mercado usando un monto en USDC.
        Calcula la cantidad automaticamente con el precio actual.
        """
        mid_px = self.get_mid_price(coin)
        qty    = round(usdc_amount / mid_px, 6)

        logger.info("MARKET BUY %s | qty=%.6f | mid_px=%.4f | usdc=%.2f",
                    coin, qty, mid_px, usdc_amount)

        resp = self.exchange.market_open(
            name=coin,
            is_buy=True,
            sz=qty,
            slippage=DEFAULT_SLIPPAGE,
        )
        return self._parse_order_response(resp)

    def market_sell(self, coin: str, qty: float) -> OrderResult:
        """
        Vende qty unidades de coin a mercado.
        """
        logger.info("MARKET SELL %s | qty=%.6f", coin, qty)
        resp = self.exchange.market_open(
            name=coin,
            is_buy=False,
            sz=qty,
            slippage=DEFAULT_SLIPPAGE,
        )
        return self._parse_order_response(resp)

    # ── ORDENES LIMITE ────────────────────────────────────────────────────────

    def limit_buy(self, coin: str, qty: float, price: float) -> OrderResult:
        """Orden limite de compra (maker — tasa 0.01%)."""
        logger.info("LIMIT BUY %s | qty=%.6f | px=%.4f", coin, qty, price)
        resp = self.exchange.order(
            name=coin,
            is_buy=True,
            sz=qty,
            limit_px=price,
            order_type={"limit": {"tif": "Gtc"}},
        )
        logger.info("RAW response limit_buy: %s", resp)
        return self._parse_order_response(resp)
    def limit_sell(self, coin: str, qty: float, price: float) -> OrderResult:
        """Orden limite de venta (maker — tasa 0.01%)."""
        logger.info("LIMIT SELL %s | qty=%.6f | px=%.4f", coin, qty, price)
        resp = self.exchange.order(
            name=coin,
            is_buy=False,
            sz=qty,
            limit_px=price,
            order_type={"limit": {"tif": "Gtc"}},
        )
        return self._parse_order_response(resp)

    def cancel_order(self, coin: str, order_id: int) -> dict:
        """Cancela una orden por ID."""
        logger.info("CANCEL %s oid=%d", coin, order_id)
        return self.exchange.cancel(name=coin, oid=order_id)

    def cancel_all_orders(self, coin: str) -> list[dict]:
        """Cancela todas las ordenes abiertas de un coin."""
        orders  = self.get_open_orders()
        results = []
        for o in orders:
            if o.get("coin", "").upper() == coin.upper():
                results.append(self.cancel_order(coin, o["oid"]))
        return results

    # ── TRANSFERENCIAS ────────────────────────────────────────────────────────

    def transfer_to_subaccount(self, usdc_amount: float) -> dict:
        """
        Transfiere USDC desde la cuenta principal hacia la subcuenta.
        Requiere que la wallet principal tenga saldo.
        """
        logger.info("TRANSFER %.2f USDC -> subcuenta %s", usdc_amount, self.subaccount[:10])
        return self.exchange.sub_account_spot_transfer(
            subaccount_user=self.subaccount,
            is_deposit=True,
            usd=usdc_amount,
        )

    # ── HELPERS INTERNOS ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_order_response(resp: Any) -> OrderResult:
        """Normaliza la respuesta del SDK a OrderResult."""
        try:
            status = resp.get("status", "unknown")

            if status != "ok":
                return OrderResult(
                    success=False, order_id=None, status=status,
                    filled_sz=0.0, avg_px=0.0, raw=resp,
                )

            data      = resp.get("response", {}).get("data", {})
            statuses  = data.get("statuses", [{}])
            first     = statuses[0] if statuses else {}

            # Hyperliquid puede devolver status:"ok" con error por orden
            # ej: {"error": "Insufficient balance"} — tratar como fallo
            if "error" in first:
                logger.error("Order rejected by exchange | error=%s | raw=%s",
                             first["error"], resp)
                return OrderResult(
                    success=False, order_id=None, status="order_error",
                    filled_sz=0.0, avg_px=0.0, raw=resp,
                )

            # puede venir como "filled" o "resting"
            filled    = first.get("filled", {})
            resting   = first.get("resting", {})

            order_id  = filled.get("oid") or resting.get("oid")
            filled_sz = float(filled.get("totalSz", 0))
            avg_px    = float(filled.get("avgPx", 0))

            logger.debug("Order OK | oid=%s status=%s raw=%s",
                         order_id, "filled" if filled else "resting", resp)

            return OrderResult(
                success=True,
                order_id=order_id,
                status="filled" if filled else "resting",
                filled_sz=filled_sz,
                avg_px=avg_px,
                raw=resp,
            )

        except Exception as e:
            logger.error("Error parseando respuesta: %s | resp=%s", e, resp)
            return OrderResult(
                success=False, order_id=None, status=f"parse_error: {e}",
                filled_sz=0.0, avg_px=0.0, raw=resp,
            )

    # ── DIAGNOSTICO ───────────────────────────────────────────────────────────

    def status(self) -> dict:
        """Resumen del estado de la subcuenta para logging/alertas."""
        balances = self.get_spot_balance()
        orders   = self.get_open_orders()
        return {
            "network":       self.network,
            "wallet":        self.wallet.address,
            "subaccount":    self.subaccount,
            "balances":      [{"coin": b.coin, "total": b.total, "available": b.available}
                              for b in balances],
            "open_orders":   len(orders),
        }
