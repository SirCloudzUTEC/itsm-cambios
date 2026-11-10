"""Respuestas HTTP con el formato del contrato de integración v1.1."""
import json
from decimal import Decimal

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Authorization,Content-Type",
    "Access-Control-Allow-Methods": "GET,POST,PUT,OPTIONS",
}


class JsonEncoder(json.JSONEncoder):
    def default(self, value):
        if isinstance(value, Decimal):
            return int(value) if value % 1 == 0 else float(value)
        return super().default(value)


class ApiError(Exception):
    """Error controlado que se traduce a {"error": {"codigo", "mensaje"}}."""

    def __init__(self, status, codigo, mensaje):
        super().__init__(mensaje)
        self.status = status
        self.codigo = codigo
        self.mensaje = mensaje


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json", **CORS_HEADERS},
        "body": json.dumps(body, cls=JsonEncoder, ensure_ascii=False),
    }


def ok(data, status_code=200, paginacion=None):
    body = {"data": data}
    if paginacion is not None:
        body["paginacion"] = paginacion
    return response(status_code, body)


def error(status_code, codigo, mensaje):
    return response(status_code, {"error": {"codigo": codigo, "mensaje": mensaje}})


def parse_body(event):
    raw = event.get("body")
    if raw in (None, ""):
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ApiError(400, "JSON_INVALIDO", "El cuerpo debe ser JSON válido.") from exc
    if not isinstance(value, dict):
        raise ApiError(400, "JSON_INVALIDO", "El cuerpo debe ser un objeto JSON.")
    return value
