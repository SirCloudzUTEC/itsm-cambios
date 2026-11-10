"""Reglas de dominio del control de cambios (RFC) y acceso a DynamoDB."""
import base64
import json
import os
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr, Key

from src.common.http import ApiError

ESTADOS = (
    "BORRADOR",
    "PENDIENTE_APROBACION",
    "APROBADO",
    "RECHAZADO",
    "PROGRAMADO",
    "IMPLEMENTADO",
)
TIPOS = ("ESTANDAR", "NORMAL", "EMERGENCIA")
RIESGOS = ("ALTO", "MEDIO", "BAJO")
EDITABLE_EN = {"BORRADOR", "PENDIENTE_APROBACION"}
CAMPOS_EDITABLES = {
    "titulo",
    "descripcion",
    "tipo",
    "riesgo",
    "plan_implementacion",
    "plan_rollback",
    "problema_id",
    "solicitud_id",
}
INDICE_ESTADO = "GSI1"       # PK tenant_id, SK estado_creado  (estado#creado_en)
INDICE_VENTANA = "GSI2"      # PK tenant_id, SK ventana_inicio (calendario; índice disperso)
LIMITE_DEFECTO = 20
LIMITE_MAXIMO = 100

_tabla = None


def tabla():
    global _tabla
    if _tabla is None:
        _tabla = boto3.resource("dynamodb").Table(os.environ["CAMBIOS_TABLE"])
    return _tabla


def reset_tabla():
    """Solo para pruebas."""
    global _tabla
    _tabla = None


# ---------------------------------------------------------------- utilidades


def ahora():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_fecha(valor, campo, fin_de_dia=False):
    """Normaliza una fecha ISO 8601 a UTC 'YYYY-MM-DDTHH:MM:SSZ'.

    Acepta también solo fecha (YYYY-MM-DD); con fin_de_dia=True se usa 23:59:59.
    """
    if not isinstance(valor, str) or not valor.strip():
        raise ApiError(400, "FECHA_INVALIDA", f"{campo} es obligatorio y debe ser ISO 8601.")
    texto = valor.strip()
    try:
        if len(texto) == 10:
            dia = datetime.strptime(texto, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            hora = "23:59:59" if fin_de_dia else "00:00:00"
            return f"{dia:%Y-%m-%d}T{hora}Z"
        fecha = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiError(400, "FECHA_INVALIDA", f"{campo} debe ser ISO 8601 (ej. 2026-11-20T02:00:00Z).") from exc
    if fecha.tzinfo is None:
        fecha = fecha.replace(tzinfo=timezone.utc)
    return fecha.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_limite(query):
    try:
        limite = int(query.get("limite", LIMITE_DEFECTO))
    except (TypeError, ValueError) as exc:
        raise ApiError(400, "LIMITE_INVALIDO", "limite debe ser un entero entre 1 y 100.") from exc
    if not 1 <= limite <= LIMITE_MAXIMO:
        raise ApiError(400, "LIMITE_INVALIDO", "limite debe ser un entero entre 1 y 100.")
    return limite


def codificar_cursor(clave):
    if not clave:
        return None
    return base64.urlsafe_b64encode(json.dumps(clave).encode()).decode().rstrip("=")


def decodificar_cursor(token, tenant_id):
    try:
        clave = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ApiError(400, "CURSOR_INVALIDO", "El cursor de paginación no es válido.") from exc
    if not isinstance(clave, dict) or clave.get("tenant_id") != tenant_id:
        raise ApiError(400, "CURSOR_INVALIDO", "El cursor no pertenece al tenant autenticado.")
    return clave


def texto_requerido(body, campo, maximo=4000):
    valor = body.get(campo)
    if not isinstance(valor, str) or not valor.strip():
        raise ApiError(400, "CAMPO_REQUERIDO", f"{campo} es obligatorio.")
    if len(valor) > maximo:
        raise ApiError(400, "CAMPO_INVALIDO", f"{campo} supera {maximo} caracteres.")
    return valor.strip()


def texto_opcional(body, campo, maximo=4000):
    valor = body.get(campo)
    if valor is None:
        return ""
    if not isinstance(valor, str) or len(valor) > maximo:
        raise ApiError(400, "CAMPO_INVALIDO", f"{campo} debe ser texto de hasta {maximo} caracteres.")
    return valor.strip()


def validar_enum(valor, opciones, campo):
    if valor not in opciones:
        raise ApiError(400, f"{campo.upper()}_INVALIDO", f"{campo} debe ser uno de: {', '.join(opciones)}.")
    return valor


def validar_referencia(valor, prefijo, tenant_id, campo):
    """IDs de otros servicios: deben ser del mismo tenant (p. ej. prb-acme-000123)."""
    if valor in (None, ""):
        return None
    if not isinstance(valor, str) or not valor.startswith(f"{prefijo}-{tenant_id}-") or len(valor) > 80:
        raise ApiError(400, f"{campo.upper()}_INVALIDO", f"{campo} debe tener el formato {prefijo}-{tenant_id}-NNNNNN.")
    return valor


# ------------------------------------------------------------------- acceso


def obtener(tenant_id, cambio_id):
    item = tabla().get_item(Key={"tenant_id": tenant_id, "cambio_id": cambio_id}).get("Item")
    if not item:
        # Un ID de otro tenant cae aquí también: 404 sin revelar que existe.
        raise ApiError(404, "NO_ENCONTRADO", "No existe un cambio con ese ID en este tenant.")
    return item


def listar(tenant_id, query):
    limite = parse_limite(query)
    params = {"Limit": limite, "ScanIndexForward": False}
    estado = query.get("estado")
    if estado:
        validar_enum(estado, ESTADOS, "estado")
        params["IndexName"] = INDICE_ESTADO
        params["KeyConditionExpression"] = Key("tenant_id").eq(tenant_id) & Key("estado_creado").begins_with(
            f"{estado}#"
        )
    else:
        params["KeyConditionExpression"] = Key("tenant_id").eq(tenant_id)

    filtros = []
    if query.get("tipo"):
        filtros.append(Attr("tipo").eq(validar_enum(query["tipo"], TIPOS, "tipo")))
    if query.get("riesgo"):
        filtros.append(Attr("riesgo").eq(validar_enum(query["riesgo"], RIESGOS, "riesgo")))
    if filtros:
        expresion = filtros[0]
        for condicion in filtros[1:]:
            expresion &= condicion
        params["FilterExpression"] = expresion
    if query.get("siguiente"):
        params["ExclusiveStartKey"] = decodificar_cursor(query["siguiente"], tenant_id)

    resultado = tabla().query(**params)
    return resultado.get("Items", []), {
        "limite": limite,
        "siguiente": codificar_cursor(resultado.get("LastEvaluatedKey")),
    }


def calendario(tenant_id, query):
    limite = parse_limite(query)
    desde = parse_fecha(query.get("desde"), "desde")
    hasta = parse_fecha(query.get("hasta"), "hasta", fin_de_dia=True)
    if desde > hasta:
        raise ApiError(400, "RANGO_INVALIDO", "desde debe ser anterior o igual a hasta.")
    params = {
        "IndexName": INDICE_VENTANA,
        "KeyConditionExpression": Key("tenant_id").eq(tenant_id) & Key("ventana_inicio").between(desde, hasta),
        "ScanIndexForward": True,
        "Limit": limite,
    }
    if query.get("siguiente"):
        params["ExclusiveStartKey"] = decodificar_cursor(query["siguiente"], tenant_id)
    resultado = tabla().query(**params)
    return resultado.get("Items", []), {
        "limite": limite,
        "siguiente": codificar_cursor(resultado.get("LastEvaluatedKey")),
    }


def transicionar(tenant_id, cambio_id, desde_estados, nuevo_estado, extra=None):
    """Cambia el estado de forma atómica: falla con 409 si alguien lo modificó antes."""
    actual = obtener(tenant_id, cambio_id)
    if actual["estado"] not in desde_estados:
        raise ApiError(
            409,
            "TRANSICION_INVALIDA",
            f"No se puede pasar de {actual['estado']} a {nuevo_estado}.",
        )
    momento = ahora()
    valores = {
        ":nuevo": nuevo_estado,
        ":orden": f"{nuevo_estado}#{actual['creado_en']}",
        ":ahora": momento,
        ":previo": actual["estado"],
    }
    nombres = {"#estado": "estado"}
    asignaciones = ["#estado = :nuevo", "estado_creado = :orden", "actualizado_en = :ahora"]
    for indice, (campo, valor) in enumerate((extra or {}).items()):
        nombres[f"#c{indice}"] = campo
        valores[f":v{indice}"] = valor
        asignaciones.append(f"#c{indice} = :v{indice}")
    resultado = tabla().update_item(
        Key={"tenant_id": tenant_id, "cambio_id": cambio_id},
        UpdateExpression="SET " + ", ".join(asignaciones),
        ConditionExpression="#estado = :previo",
        ExpressionAttributeNames=nombres,
        ExpressionAttributeValues=valores,
        ReturnValues="ALL_NEW",
    )
    return resultado["Attributes"]
