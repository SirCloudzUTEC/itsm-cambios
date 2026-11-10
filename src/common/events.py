"""Publicador de eventos de dominio (SNS).

Fase actual (contrato v1.1, avance 30%): SNS/SQS están POSPUESTOS. Con
EVENTS_ENABLED distinto de "true" el publicador es un no-op: registra el evento en
CloudWatch y retorna sin error, de modo que nunca rompe una operación CRUD.

Cuando se autorice la fase EDA: desplegar itsm-infra, añadir en serverless.yml
SNS_EVENTOS_ARN: ${ssm:/itsm/${sls:stage}/sns-eventos-arn} y EVENTS_ENABLED: "true".
"""
import json
import logging
import os
from datetime import datetime, timezone

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)


def eventos_habilitados():
    return os.environ.get("EVENTS_ENABLED", "false").strip().lower() == "true"


def sobre(evento, tenant_id, data):
    return {
        "evento": evento,
        "version": "1.0",
        "tenant_id": tenant_id,
        "origen": "cambios",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data": data,
    }


def publicar(evento, tenant_id, data):
    """Publica el evento o, si los eventos están desactivados, no hace nada.

    Nunca lanza excepción: el registro en DynamoDB ya se guardó y un fallo del bus no
    debe convertirse en un 500 para el cliente.
    """
    mensaje = sobre(evento, tenant_id, data)
    if not eventos_habilitados():
        LOGGER.info("evento_omitido %s", json.dumps({"motivo": "EVENTS_ENABLED=false", **mensaje}))
        return None

    topic_arn = os.environ.get("SNS_EVENTOS_ARN")
    if not topic_arn:
        LOGGER.warning("evento_omitido %s", json.dumps({"motivo": "SNS_EVENTOS_ARN vacío", **mensaje}))
        return None

    try:
        import boto3

        boto3.client("sns").publish(
            TopicArn=topic_arn,
            Message=json.dumps(mensaje),
            MessageAttributes={
                "evento": {"DataType": "String", "StringValue": evento},
                "tenant_id": {"DataType": "String", "StringValue": tenant_id},
            },
        )
    except Exception:  # noqa: BLE001 - el CRUD no debe fallar por el bus
        LOGGER.exception("evento_fallido %s", json.dumps(mensaje))
        return None
    LOGGER.info("evento_publicado %s", json.dumps(mensaje))
    return mensaje
