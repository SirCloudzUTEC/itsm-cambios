"""Contexto de seguridad entregado por el Lambda Authorizer compartido de itsm-auth.

El tenant SIEMPRE sale del contexto del authorizer (JWT validado), nunca del body,
query string ni path.
"""
import re

from src.common.http import ApiError

ROLES_VALIDOS = {"ADMIN", "AGENTE", "SOLICITANTE"}
TENANT_RE = re.compile(r"^[a-z0-9-]{2,40}$")


def contexto(event):
    authorizer = (event.get("requestContext") or {}).get("authorizer") or {}
    # REST + token authorizer: claves planas. HTTP API: anidadas en "lambda".
    if not authorizer.get("tenant_id") and isinstance(authorizer.get("lambda"), dict):
        authorizer = authorizer["lambda"]

    ctx = {
        "tenant_id": authorizer.get("tenant_id"),
        "usuario_id": authorizer.get("usuario_id"),
        "rol": authorizer.get("rol"),
    }
    if not all(ctx.values()) or ctx["rol"] not in ROLES_VALIDOS:
        raise ApiError(401, "NO_AUTORIZADO", "El token no trae tenant_id, usuario_id o rol válidos.")
    if not TENANT_RE.fullmatch(str(ctx["tenant_id"])):
        raise ApiError(403, "TENANT_INVALIDO", "El tenant del token no es válido.")
    return ctx


def exigir_rol(ctx, *roles):
    if ctx["rol"] not in roles:
        raise ApiError(
            403,
            "ROL_NO_PERMITIDO",
            f"El rol {ctx['rol']} no puede realizar esta acción (requiere {' o '.join(roles)}).",
        )
