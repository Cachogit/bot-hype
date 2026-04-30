# -*- coding: utf-8 -*-
import os

ASSET         = "HYPE"
N_LEVELS      = 15
GRID_LOW      = 38.00
LEVEL_SPACING = 0.23    # distancia entre niveles en USD
CAPITAL_USDC     = float(os.getenv("CAPITAL_PER_LEVEL", "266.0"))
MAX_CAPITAL_USDC = float(os.getenv("MAX_CAPITAL_USDC", "4000.0"))
MAX_AUTO_SHIFTS  = 3        # shifts automáticos permitidos antes de requerir /shift_up manual
TAKER_FEE        = 0.0005
MAKER_FEE        = 0.0001
SZ_DECIMALS      = 2        # decimales permitidos para qty en HYPE spot (szDecimals=2)

# Venta siempre en el nivel siguiente: nivel_compra + LEVEL_SPACING
# (no hay porcentaje fijo — el target es determinista y alineado a la grilla)

GRID_HIGH = round(GRID_LOW + (N_LEVELS - 1) * LEVEL_SPACING, 2)

LEVELS = [round(GRID_LOW + i * LEVEL_SPACING, 2) for i in range(N_LEVELS)]
