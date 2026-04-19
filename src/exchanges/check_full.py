# -*- coding: utf-8 -*-
"""
Verificacion completa: Hyperliquid + Anthropic + Telegram con mensaje de prueba.
"""
import sys, io, os, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from dotenv import load_dotenv
load_dotenv()

import requests
from datetime import datetime

SEP  = "=" * 58
SEP2 = "-" * 58

def ok(label, detail=""):
    print(f"  [OK]   {label}" + (f": {detail}" if detail else ""))

def fail(label, err):
    print(f"  [FAIL] {label}: {err}")

def section(title):
    print(f"\n[{title}]")

# ── 1. VARIABLES DE ENTORNO ────────────────────────────────────────────────────
print(f"\n{SEP}")
print("  VERIFICACION COMPLETA  --  HYPE Trading Bot")
print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print(SEP)

section("1. Variables de entorno")
vars_cfg = {
    "HYPERLIQUID_PRIVATE_KEY":        ("critica",  "0x"),
    "HYPERLIQUID_SUBACCOUNT_ADDRESS": ("critica",  "0x"),
    "HYPERLIQUID_NETWORK":            ("critica",  None),
    "ANTHROPIC_API_KEY":              ("critica",  "sk-ant"),
    "TELEGRAM_BOT_TOKEN":             ("critica",  ":"),
    "TELEGRAM_CHAT_ID":               ("critica",  None),
}

all_ok = True
for key, (level, prefix) in vars_cfg.items():
    val = os.getenv(key, "")
    missing = not val or val.startswith("0xTU_") or val.startswith("123456789:AA")
    if missing:
        fail(key, "NO CONFIGURADA")
        all_ok = False
    else:
        masked = val[:8] + "..." + val[-4:] if len(val) > 14 else val
        ok(key, masked)

if not all_ok:
    print(f"\n  Faltan variables criticas.\n{SEP}\n")
    sys.exit(1)

# ── 2. HYPERLIQUID ─────────────────────────────────────────────────────────────
section("2. Hyperliquid SDK")
hl_client = None
try:
    from src.exchanges.hyperliquid_client import HyperliquidClient
    hl_client = HyperliquidClient.from_env()
    ok("SDK instanciado", f"red={hl_client.network}")
    ok("Wallet", hl_client.wallet.address)
    ok("Subcuenta", hl_client.subaccount)
except Exception as e:
    fail("SDK", e)

if hl_client:
    try:
        price = hl_client.get_mid_price("HYPE")
        ok("Precio HYPE en vivo", f"${price:.4f}")
    except Exception as e:
        fail("Precio HYPE", e)

    try:
        usdc = hl_client.get_usdc_balance()
        ok("USDC en subcuenta", f"${usdc:.2f}")
    except Exception as e:
        fail("Balance subcuenta", e)

    try:
        orders = hl_client.get_open_orders()
        ok("Ordenes abiertas", f"{len(orders)}")
    except Exception as e:
        fail("Ordenes abiertas", e)

# ── 3. ANTHROPIC ───────────────────────────────────────────────────────────────
section("3. Anthropic API")
anthropic_ok = False
try:
    import anthropic
    client_ai = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    msg = client_ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
        messages=[{"role": "user", "content": "responde solo: OK"}],
    )
    ok("Conexion API", f"respuesta: '{msg.content[0].text.strip()}'")
    anthropic_ok = True
except Exception as e:
    fail("Anthropic API", e)

# ── 4. TELEGRAM ────────────────────────────────────────────────────────────────
section("4. Telegram Bot")
token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
base    = f"https://api.telegram.org/bot{token}"

# 4a. Verificar bot
bot_name = None
try:
    r = requests.get(f"{base}/getMe", timeout=10)
    data = r.json()
    if data.get("ok"):
        bot_name = data["result"]["username"]
        ok("Bot autenticado", f"@{bot_name}")
    else:
        fail("getMe", data.get("description", "error desconocido"))
except Exception as e:
    fail("getMe", e)

# 4b. Enviar mensaje de prueba
if bot_name and hl_client:
    price = hl_client.get_mid_price("HYPE") if hl_client else 0
    usdc  = hl_client.get_usdc_balance() if hl_client else 0
    msg_text = (
        f"*HYPE Trading Bot — Conexion verificada*\n\n"
        f"Fecha/Hora: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`\n"
        f"Red: `{hl_client.network}`\n"
        f"Subcuenta: `{hl_client.subaccount[:10]}...{hl_client.subaccount[-4:]}`\n\n"
        f"Precio HYPE: `${price:.4f}`\n"
        f"USDC subcuenta: `${usdc:.2f}`\n\n"
        f"Hyperliquid SDK: OK\n"
        f"Anthropic API: {'OK' if anthropic_ok else 'PENDIENTE'}\n"
        f"Telegram Bot: OK\n\n"
        f"Bot listo para monitorear zonas de soporte."
    )
    try:
        r = requests.post(
            f"{base}/sendMessage",
            json={"chat_id": chat_id, "text": msg_text, "parse_mode": "Markdown"},
            timeout=10,
        )
        data = r.json()
        if data.get("ok"):
            msg_id = data["result"]["message_id"]
            ok("Mensaje enviado", f"message_id={msg_id} | chat_id={chat_id}")
        else:
            fail("sendMessage", data.get("description", "error"))
    except Exception as e:
        fail("sendMessage", e)
elif bot_name:
    fail("Mensaje de prueba", "no se pudo enviar (cliente HL no disponible)")

# ── RESUMEN ────────────────────────────────────────────────────────────────────
print(f"\n{SEP}")
print("  Verificacion completa finalizada.")
print(f"{SEP}\n")
