"""API REST del microservicio Cambios (RFC) — itsm-cambios.

Ciclo de vida:
    BORRADOR --enviar--> PENDIENTE_APROBACION --aprobar--> APROBADO --programar--> PROGRAMADO --implementar--> IMPLEMENTADO
                                             \\--rechazar--> RECHAZADO
    (PROGRAMADO --programar--> PROGRAMADO permite reprogramar la ventana)

Acceso: ADMIN y AGENTE. aprobar/rechazar: solo ADMIN. SOLICITANTE recibe 403.
"""
import json
import logging
import uuid

from botocore.exceptions import ClientError

from src.common import modelo
from src.common.auth import contexto, exigir_rol
from src.common.events import publicar
from src.common.http import ApiError, error, ok, parse_body

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

GESTION = ("ADMIN", "AGENTE")


def _log(accion, ctx, **datos):
    LOGGER.info(
        "accion %s",
        json.dumps({"accion": accion, "tenant_id": ctx["tenant_id"], "usuario_id": ctx["usuario_id"], **datos}),
    )


def _id_path(event):
    cambio_id = (event.get("pathParameters") or {}).get("id")
    if not cambio_id or len(cambio_id) > 80:
        raise ApiError(400, "ID_REQUERIDO", "Falta el identificador del cambio.")
    return cambio_id


# ------------------------------------------------------------------ handlers


def _crear(event, ctx):
    tenant_id = ctx["tenant_id"]
    body = parse_body(event)
    momento = modelo.ahora()
    estado = "BORRADOR"
    item = {
        "tenant_id": tenant_id,
        "cambio_id": f"chg-{tenant_id}-{uuid.uuid4().hex[:12]}",
        "titulo": modelo.texto_requerido(body, "titulo", 200),
        "descripcion": modelo.texto_opcional(body, "descripcion"),
        "tipo": modelo.validar_enum(body.get("tipo", "NORMAL"), modelo.TIPOS, "tipo"),
        "riesgo": modelo.validar_enum(body.get("riesgo", "MEDIO"), modelo.RIESGOS, "riesgo"),
        "estado": estado,
        "estado_creado": f"{estado}#{momento}",
        "plan_implementacion": modelo.texto_opcional(body, "plan_implementacion"),
        "plan_rollback": modelo.texto_opcional(body, "plan_rollback"),
        "solicitante_id": ctx["usuario_id"],
        "aprobador_id": None,
        "motivo_rechazo": None,
        "problema_id": modelo.validar_referencia(body.get("problema_id"), "prb", tenant_id, "problema_id"),
        "solicitud_id": modelo.validar_referencia(body.get("solicitud_id"), "req", tenant_id, "solicitud_id"),
        "creado_en": momento,
        "actualizado_en": momento,
    }
    # ventana_inicio / ventana_fin se omiten hasta programar: el GSI2 es disperso.
    modelo.tabla().put_item(Item=item, ConditionExpression="attribute_not_exists(cambio_id)")
    _log("cambio.creado", ctx, cambio_id=item["cambio_id"])
    return ok(item, 201)


def _editar(event, ctx, cambio_id):
    tenant_id = ctx["tenant_id"]
    body = parse_body(event)
    desconocidos = set(body) - modelo.CAMPOS_EDITABLES
    if desconocidos:
        raise ApiError(400, "CAMPOS_NO_PERMITIDOS", f"No se pueden editar: {', '.join(sorted(desconocidos))}.")
    if not body:
        raise ApiError(400, "SIN_CAMPOS", "Indica al menos un campo editable.")

    cambios = {}
    if "titulo" in body:
        cambios["titulo"] = modelo.texto_requerido(body, "titulo", 200)
    for campo in ("descripcion", "plan_implementacion", "plan_rollback"):
        if campo in body:
            cambios[campo] = modelo.texto_opcional(body, campo)
    if "tipo" in body:
        cambios["tipo"] = modelo.validar_enum(body["tipo"], modelo.TIPOS, "tipo")
    if "riesgo" in body:
        cambios["riesgo"] = modelo.validar_enum(body["riesgo"], modelo.RIESGOS, "riesgo")
    if "problema_id" in body:
        cambios["problema_id"] = modelo.validar_referencia(body["problema_id"], "prb", tenant_id, "problema_id")
    if "solicitud_id" in body:
        cambios["solicitud_id"] = modelo.validar_referencia(body["solicitud_id"], "req", tenant_id, "solicitud_id")

    actual = modelo.obtener(tenant_id, cambio_id)
    if actual["estado"] not in modelo.EDITABLE_EN:
        raise ApiError(409, "NO_EDITABLE", f"Solo se edita en BORRADOR o PENDIENTE_APROBACION (actual: {actual['estado']}).")

    nombres = {"#estado": "estado"}
    valores = {":ahora": modelo.ahora(), ":previo": actual["estado"]}
    asignaciones = ["actualizado_en = :ahora"]
    for indice, (campo, valor) in enumerate(cambios.items()):
        nombres[f"#c{indice}"] = campo
        valores[f":v{indice}"] = valor
        asignaciones.append(f"#c{indice} = :v{indice}")
    resultado = modelo.tabla().update_item(
        Key={"tenant_id": tenant_id, "cambio_id": cambio_id},
        UpdateExpression="SET " + ", ".join(asignaciones),
        ConditionExpression="#estado = :previo",
        ExpressionAttributeNames=nombres,
        ExpressionAttributeValues=valores,
        ReturnValues="ALL_NEW",
    )
    _log("cambio.editado", ctx, cambio_id=cambio_id, campos=sorted(cambios))
    return ok(resultado["Attributes"])


def _enviar(ctx, cambio_id):
    actual = modelo.obtener(ctx["tenant_id"], cambio_id)
    faltantes = [c for c in ("plan_implementacion", "plan_rollback") if not (actual.get(c) or "").strip()]
    if faltantes and actual["estado"] == "BORRADOR":
        raise ApiError(400, "PLAN_INCOMPLETO", f"Antes de enviar completa: {', '.join(faltantes)}.")
    item = modelo.transicionar(ctx["tenant_id"], cambio_id, {"BORRADOR"}, "PENDIENTE_APROBACION")
    _log("cambio.enviado", ctx, cambio_id=cambio_id)
    return ok(item)


def _aprobar(ctx, cambio_id):
    exigir_rol(ctx, "ADMIN")
    item = modelo.transicionar(
        ctx["tenant_id"],
        cambio_id,
        {"PENDIENTE_APROBACION"},
        "APROBADO",
        {"aprobador_id": ctx["usuario_id"], "aprobado_en": modelo.ahora()},
    )
    publicar("cambio.aprobado", ctx["tenant_id"], {"cambio_id": cambio_id, "aprobador_id": ctx["usuario_id"]})
    _log("cambio.aprobado", ctx, cambio_id=cambio_id)
    return ok(item)


def _rechazar(event, ctx, cambio_id):
    exigir_rol(ctx, "ADMIN")
    motivo = modelo.texto_requerido(parse_body(event), "motivo", 1000)
    item = modelo.transicionar(
        ctx["tenant_id"],
        cambio_id,
        {"PENDIENTE_APROBACION"},
        "RECHAZADO",
        {"aprobador_id": ctx["usuario_id"], "motivo_rechazo": motivo},
    )
    _log("cambio.rechazado", ctx, cambio_id=cambio_id)
    return ok(item)


def _programar(event, ctx, cambio_id):
    body = parse_body(event)
    inicio = modelo.parse_fecha(body.get("ventana_inicio"), "ventana_inicio")
    fin = modelo.parse_fecha(body.get("ventana_fin"), "ventana_fin")
    if fin <= inicio:
        raise ApiError(400, "VENTANA_INVALIDA", "ventana_fin debe ser posterior a ventana_inicio.")
    item = modelo.transicionar(
        ctx["tenant_id"],
        cambio_id,
        {"APROBADO", "PROGRAMADO"},
        "PROGRAMADO",
        {"ventana_inicio": inicio, "ventana_fin": fin},
    )
    _log("cambio.programado", ctx, cambio_id=cambio_id, ventana_inicio=inicio, ventana_fin=fin)
    return ok(item)


def _implementar(event, ctx, cambio_id):
    notas = modelo.texto_opcional(parse_body(event), "notas_implementacion", 2000)
    item = modelo.transicionar(
        ctx["tenant_id"],
        cambio_id,
        {"PROGRAMADO"},
        "IMPLEMENTADO",
        {"implementado_en": modelo.ahora(), "implementado_por": ctx["usuario_id"], "notas_implementacion": notas},
    )
    # Contrato con Problemas y Solicitudes: data incluye problema_id y solicitud_id (pueden ser null).
    publicar(
        "cambio.implementado",
        ctx["tenant_id"],
        {
            "cambio_id": cambio_id,
            "problema_id": item.get("problema_id"),
            "solicitud_id": item.get("solicitud_id"),
        },
    )
    _log("cambio.implementado", ctx, cambio_id=cambio_id)
    return ok(item)


# ------------------------------------------------------------------- router


def _enrutar(event):
    ctx = contexto(event)
    exigir_rol(ctx, *GESTION)
    metodo = (event.get("httpMethod") or "").upper()
    ruta = event.get("resource") or ""
    query = event.get("queryStringParameters") or {}

    if ruta == "/cambios" and metodo == "GET":
        items, paginacion = modelo.listar(ctx["tenant_id"], query)
        return ok(items, paginacion=paginacion)
    if ruta == "/cambios" and metodo == "POST":
        return _crear(event, ctx)
    if ruta == "/cambios/calendario" and metodo == "GET":
        items, paginacion = modelo.calendario(ctx["tenant_id"], query)
        return ok(items, paginacion=paginacion)

    cambio_id = _id_path(event)
    if ruta == "/cambios/{id}" and metodo == "GET":
        return ok(modelo.obtener(ctx["tenant_id"], cambio_id))
    if ruta == "/cambios/{id}" and metodo == "PUT":
        return _editar(event, ctx, cambio_id)
    if metodo == "POST":
        if ruta == "/cambios/{id}/enviar":
            return _enviar(ctx, cambio_id)
        if ruta == "/cambios/{id}/aprobar":
            return _aprobar(ctx, cambio_id)
        if ruta == "/cambios/{id}/rechazar":
            return _rechazar(event, ctx, cambio_id)
        if ruta == "/cambios/{id}/programar":
            return _programar(event, ctx, cambio_id)
        if ruta == "/cambios/{id}/implementar":
            return _implementar(event, ctx, cambio_id)
    raise ApiError(404, "RUTA_NO_ENCONTRADA", "La ruta solicitada no existe.")


def handler(event, _context):
    try:
        return _enrutar(event)
    except ApiError as exc:
        if exc.status >= 500:
            LOGGER.error("error_api %s %s", exc.codigo, exc.mensaje)
        return error(exc.status, exc.codigo, exc.mensaje)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return error(409, "CONFLICTO", "El cambio fue modificado por otra operación; vuelve a consultarlo.")
        LOGGER.exception("error_dynamodb")
        return error(500, "ERROR_INTERNO", "Error al acceder a la base de datos.")
    except Exception:  # noqa: BLE001
        LOGGER.exception("error_no_controlado")
        return error(500, "ERROR_INTERNO", "Ocurrió un error inesperado.")
