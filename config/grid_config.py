# -*- coding: utf-8 -*-
ASSET         = "HYPE"
N_LEVELS      = 20
GRID_LOW      = 38.00
LEVEL_SPACING = 0.23    # distancia entre niveles en USD
CAPITAL_USDC  = 50.0    # USDC por nivel  (20 × $50 = $1.000 total)
TAKER_FEE     = 0.0005
MAKER_FEE     = 0.0001

# Venta siempre en el nivel siguiente: nivel_compra + LEVEL_SPACING
# (no hay porcentaje fijo — el target es determinista y alineado a la grilla)

GRID_HIGH = round(GRID_LOW + (N_LEVELS - 1) * LEVEL_SPACING, 2)

LEVELS = [round(GRID_LOW + i * LEVEL_SPACING, 2) for i in range(N_LEVELS)]
