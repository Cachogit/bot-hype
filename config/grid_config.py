# -*- coding: utf-8 -*-
import os

ASSET             = "HYPE"
N_LEVELS          = 6
FIRST_LEVEL_PCT   = 0.003   # primer nivel a 0.3% bajo el precio de referencia
LEVEL_SPACING_PCT = 0.006   # separación de 0.6% entre niveles siguientes
CAPITAL_USDC      = float(os.getenv("CAPITAL_PER_LEVEL", "900.0"))
MAX_CAPITAL_USDC  = float(os.getenv("MAX_CAPITAL_USDC",  "5400.0"))
MAX_AUTO_SHIFTS   = 3
TAKER_FEE         = 0.0005
MAKER_FEE         = 0.0001
SZ_DECIMALS       = 2


def calc_levels(ref_price: float) -> list:
    """Calcula N_LEVELS precios de compra bajo ref_price.
    Primer nivel a FIRST_LEVEL_PCT bajo el precio; los siguientes
    separados LEVEL_SPACING_PCT entre sí. Retorna lista de menor a mayor.
    """
    first = round(ref_price * (1 - FIRST_LEVEL_PCT), 2)
    levels = [first]
    for _ in range(N_LEVELS - 1):
        levels.append(round(levels[-1] * (1 - LEVEL_SPACING_PCT), 2))
    return sorted(levels)
