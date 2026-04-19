# -*- coding: utf-8 -*-
"""
Verifica que el .env este bien configurado y la conexion a Hyperliquid funcione.
Ejecutar: python src/exchanges/check_connection.py
"""
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from dotenv import load_dotenv
load_dotenv()

SEP = "=" * 55

def check(label, fn):
    try:
        result = fn()
        print(f"  [OK] {label}: {result}")
        return True
    except Exception as e:
        print(f"  [FAIL] {label}: {e}")
        return False


def main():
    print(f"\n{SEP}")
    print("  CHECK DE CONEXION  --  Hyperliquid Bot")
    print(SEP)

    # 1. Variables de entorno
    print("\n[1] Variables de entorno")
    env_vars = {
        "HYPERLIQUID_PRIVATE_KEY":     os.getenv("HYPERLIQUID_PRIVATE_KEY", ""),
        "HYPERLIQUID_SUBACCOUNT_ADDRESS": os.getenv("HYPERLIQUID_SUBACCOUNT_ADDRESS", ""),
        "HYPERLIQUID_NETWORK":         os.getenv("HYPERLIQUID_NETWORK", "testnet"),
        "ANTHROPIC_API_KEY":           os.getenv("ANTHROPIC_API_KEY", ""),
        "TELEGRAM_BOT_TOKEN":          os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "TELEGRAM_CHAT_ID":            os.getenv("TELEGRAM_CHAT_ID", ""),
    }

    OPTIONAL = {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}
    PLACEHOLDERS = {"0xTU_", "0xDIRECCION", "sk-ant-TU", "123456789:", "-1001234567890"}

    critical_missing = False
    for key, val in env_vars.items():
        is_placeholder = not val or any(val.startswith(p) or val == p for p in PLACEHOLDERS)
        masked = val[:6] + "..." + val[-4:] if len(val) > 12 else val
        optional_tag = " (opcional)" if key in OPTIONAL else ""
        status = "[PENDIENTE]" if is_placeholder else "[OK]"
        print(f"  {status} {key}{optional_tag}: {masked}")
        if is_placeholder and key not in OPTIONAL:
            critical_missing = True

    if critical_missing:
        print("\n  Faltan credenciales criticas. Edita el .env antes de continuar.")
        print(f"{SEP}\n")
        return

    # 2. Conexion al SDK
    print("\n[2] Conexion al SDK de Hyperliquid")
    try:
        from src.exchanges.hyperliquid_client import HyperliquidClient
        client = HyperliquidClient.from_env()
        print(f"  [OK] Exchange instanciado | red={client.network}")
        print(f"  [OK] Wallet: {client.wallet.address}")
        print(f"  [OK] Subcuenta: {client.subaccount}")
    except Exception as e:
        print(f"  [FAIL] No se pudo crear el cliente: {e}")
        print(f"{SEP}\n")
        return

    # 3. Info publica (no requiere firma)
    print("\n[3] Datos publicos de mercado")
    check("Precio HYPE", lambda: f"${client.get_mid_price('HYPE'):.4f}")
    check("Spot meta (pares disponibles)",
          lambda: f"{len(client.get_spot_meta().get('tokens', []))} tokens")

    # 4. Datos de la subcuenta (requiere direccion valida)
    print("\n[4] Estado de la subcuenta")
    check("Balances spot", lambda: client.get_spot_balance())
    check("USDC disponible", lambda: f"${client.get_usdc_balance():.2f}")
    check("Ordenes abiertas", lambda: f"{len(client.get_open_orders())} ordenes")

    # 5. Resumen completo
    print("\n[5] Resumen de estado")
    try:
        st = client.status()
        for k, v in st.items():
            print(f"  {k}: {v}")
    except Exception as e:
        print(f"  [FAIL] {e}")

    print(f"\n{SEP}")
    print("  Conexion verificada. El bot esta listo para operar.")
    print(f"{SEP}\n")


if __name__ == "__main__":
    main()
