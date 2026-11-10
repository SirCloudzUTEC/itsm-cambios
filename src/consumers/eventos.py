"""Consumidor de la cola q-cambios — CÓDIGO FUTURO, FUERA DEL DESPLIEGUE ACTUAL.

Contrato v1.1: SNS/SQS están pospuestos en el avance del 30%. Esta función NO está
declarada en serverless.yml. Cuando el profesor autorice la fase EDA, añadir:

    consumirQCambios:
      handler: src.consumers.eventos.handler
      events:
        - sqs:
            arn: ${ssm:/itsm/${sls:stage}/q-cambios-arn}
            batchSize: 10
            functionResponseType: ReportBatchItemFailures

Eventos que procesa (filtro de q-cambios en itsm-infra):
  * problema.causa_raiz_identificada -> crea RFC en BORRADOR vinculada al problema.
  * solicitud.cambio_estandar        -> crea cambio ESTANDAR ya APROBADO vinculado a la solicitud.

Idempotente: el cambio_id se deriva del origen, así un mensaje duplicado (SQS entrega
al menos una vez) no crea dos cambios.
"""
import hashlib
import json
import logging

from botocore.exceptions import ClientError

from src.common import modelo

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)


def _leer_sobre(record):
    cuerpo = json.loads(record["body"])
    if isinstance(cuerpo, dict) and "Message" in cuerpo and "evento" not in cuerpo:
        return json.loads(cuerpo["Message"])  # sobre SNS estándar
    return cuerpo  # RawMessageDelivery


def _id_derivado(tenant_id, origen):
    return f"chg-{tenant_id}-e{hashlib.sha1(origen.encode()).hexdigest()[:11]}"


def _crear_si_no_existe(item):
    try:
        modelo.tabla().put_item(Item=item, ConditionExpression="attribute_not_exists(cambio_id)")
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            LOGGER.info("duplicado_ignorado %s", item["cambio_id"])
            return False
        raise


def _base(tenant_id, cambio_id, estado, momento):
    return {
        "tenant_id": tenant_id,
        "cambio_id": cambio_id,
        "estado": estado,
        "estado_creado": f"{estado}#{momento}",
        "aprobador_id": None,
        "motivo_rechazo": None,
        "problema_id": None,
        "solicitud_id": None,
        "creado_en": momento,
        "actualizado_en": momento,
    }


def procesar(sobre):
    evento = sobre.get("evento")
    tenant_id = sobre.get("tenant_id")
    data = sobre.get("data") or {}
    momento = modelo.ahora()

    if evento == "problema.causa_raiz_identificada":
        problema_id = data.get("problema_id")
        if not tenant_id or not str(problema_id or "").startswith(f"prb-{tenant_id}-"):
            LOGGER.warning("evento_invalido %s", json.dumps(sobre))
            return None
        item = _base(tenant_id, _id_derivado(tenant_id, f"problema:{problema_id}"), "BORRADOR", momento)
        item.update(
            {
                "titulo": f"RFC por causa raíz de {problema_id}",
                "descripcion": str(data.get("causa_raiz") or "")[:4000],
                "tipo": "NORMAL",
                "riesgo": "MEDIO",
                "plan_implementacion": "",
                "plan_rollback": "",
                "solicitante_id": "sistema",
                "problema_id": problema_id,
            }
        )
        return item if _crear_si_no_existe(item) else None

    if evento == "solicitud.cambio_estandar":
        solicitud_id = data.get("solicitud_id")
        if not tenant_id or not str(solicitud_id or "").startswith(f"req-{tenant_id}-"):
            LOGGER.warning("evento_invalido %s", json.dumps(sobre))
            return None
        item = _base(tenant_id, _id_derivado(tenant_id, f"solicitud:{solicitud_id}"), "APROBADO", momento)
        item.update(
            {
                "titulo": f"Cambio estándar para {solicitud_id}",
                "descripcion": f"Servicio de catálogo {data.get('servicio_id', '')} marcado como cambio estándar.",
                "tipo": "ESTANDAR",
                "riesgo": "BAJO",
                "plan_implementacion": "Procedimiento estándar preaprobado del catálogo.",
                "plan_rollback": "Procedimiento estándar de reversión del catálogo.",
                "solicitante_id": "sistema",
                "aprobador_id": "preaprobado",
                "aprobado_en": momento,
                "solicitud_id": solicitud_id,
            }
        )
        return item if _crear_si_no_existe(item) else None

    LOGGER.info("evento_ignorado %s", evento)
    return None


def handler(event, _context):
    fallidos = []
    for record in event.get("Records", []):
        try:
            procesar(_leer_sobre(record))
        except Exception:  # noqa: BLE001
            LOGGER.exception("error_consumidor %s", record.get("messageId"))
            fallidos.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": fallidos}
