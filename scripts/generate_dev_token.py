"""Genera un JWT HS256 de prueba con los claims del contrato (sub, tenant_id, rol, email, iat, exp).

Solo para pruebas manuales contra la API desplegada cuando se conoce el JWT_SECRET de
itsm-auth (no lo subas a Git ni lo compartas por el grupo). Lo normal es obtener el
token con POST /auth/login de itsm-auth.

    JWT_SECRET=... python scripts/generate_dev_token.py --tenant acme --rol ADMIN
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import time


def _b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def firmar(claims, secreto):
    cabecera = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    cuerpo = _b64(json.dumps(claims, separators=(",", ":")).encode())
    firma = hmac.new(secreto.encode(), f"{cabecera}.{cuerpo}".encode(), hashlib.sha256).digest()
    return f"{cabecera}.{cuerpo}.{_b64(firma)}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant", default="acme")
    parser.add_argument("--usuario", help="por defecto usr-{tenant}-00001")
    parser.add_argument("--rol", choices=("ADMIN", "AGENTE", "SOLICITANTE"), default="ADMIN")
    args = parser.parse_args()
    secreto = os.environ.get("JWT_SECRET")
    if not secreto:
        parser.error("Define la variable de entorno JWT_SECRET")
    usuario = args.usuario or f"usr-{args.tenant}-00001"
    ahora = int(time.time())
    print(
        firmar(
            {
                "sub": usuario,
                "tenant_id": args.tenant,
                "rol": args.rol,
                "email": f"{usuario}@{args.tenant}.test",
                "iat": ahora,
                "exp": ahora + 8 * 3600,
            },
            secreto,
        )
    )


if __name__ == "__main__":
    main()
