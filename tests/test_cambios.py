"""Pruebas del contrato de Cambios contra DynamoDB simulado (moto). No requieren AWS.

    pip install -r requirements-dev.txt
    python -m unittest discover -s tests -v
"""
import json
import os
import sys
import unittest
from pathlib import Path

os.environ.update(
    {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_EC2_METADATA_DISABLED": "true",
        "CAMBIOS_TABLE": "itsm-cambios-test-cambios",
        "EVENTS_ENABLED": "false",
    }
)
os.environ.pop("SNS_EVENTOS_ARN", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

from seed import seed_cambios  # noqa: E402
from src.common import modelo  # noqa: E402
from src.consumers import eventos  # noqa: E402
from src.handlers import cambios  # noqa: E402


def crear_tabla():
    boto3.client("dynamodb").create_table(
        TableName=os.environ["CAMBIOS_TABLE"],
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": n, "AttributeType": "S"}
            for n in ("tenant_id", "cambio_id", "estado_creado", "ventana_inicio")
        ],
        KeySchema=[
            {"AttributeName": "tenant_id", "KeyType": "HASH"},
            {"AttributeName": "cambio_id", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": nombre,
                "KeySchema": [
                    {"AttributeName": "tenant_id", "KeyType": "HASH"},
                    {"AttributeName": sk, "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
            for nombre, sk in (("GSI1", "estado_creado"), ("GSI2", "ventana_inicio"))
        ],
    )


def evento(metodo, recurso, tenant="acme", rol="ADMIN", usuario=None, id=None, body=None, query=None):
    return {
        "httpMethod": metodo,
        "resource": recurso,
        "pathParameters": {"id": id} if id else None,
        "queryStringParameters": query,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {
            "authorizer": {
                "tenant_id": tenant,
                "usuario_id": usuario or f"usr-{tenant}-00001",
                "rol": rol,
            }
        },
    }


def llamar(*args, **kwargs):
    resultado = cambios.handler(evento(*args, **kwargs), None)
    return resultado["statusCode"], json.loads(resultado["body"])


RFC = {
    "titulo": "Actualizar firmware del firewall",
    "tipo": "NORMAL",
    "riesgo": "ALTO",
    "plan_implementacion": "Aplicar firmware 7.2",
    "plan_rollback": "Restaurar 7.0",
    "problema_id": "prb-acme-000123",
}


class BaseTest(unittest.TestCase):
    def setUp(self):
        self.mock = mock_aws()
        self.mock.start()
        modelo.reset_tabla()
        crear_tabla()

    def tearDown(self):
        self.mock.stop()
        modelo.reset_tabla()

    def crear(self, tenant="acme", **extra):
        status, body = llamar("POST", "/cambios", tenant=tenant, body={**RFC, **extra} if tenant == "acme" else {
            **RFC, "problema_id": None, **extra})
        self.assertEqual(status, 201, body)
        return body["data"]


class SeguridadTest(BaseTest):
    def test_sin_contexto_de_authorizer_responde_401(self):
        resultado = cambios.handler({"httpMethod": "GET", "resource": "/cambios", "requestContext": {}}, None)
        self.assertEqual(resultado["statusCode"], 401)
        self.assertIn("error", json.loads(resultado["body"]))

    def test_solicitante_recibe_403(self):
        status, body = llamar("GET", "/cambios", rol="SOLICITANTE")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["codigo"], "ROL_NO_PERMITIDO")

    def test_agente_no_puede_aprobar_ni_rechazar(self):
        cambio = self.crear()
        llamar("POST", "/cambios/{id}/enviar", id=cambio["cambio_id"], rol="AGENTE")
        self.assertEqual(llamar("POST", "/cambios/{id}/aprobar", id=cambio["cambio_id"], rol="AGENTE")[0], 403)
        self.assertEqual(
            llamar("POST", "/cambios/{id}/rechazar", id=cambio["cambio_id"], rol="AGENTE", body={"motivo": "x"})[0],
            403,
        )

    def test_id_de_otro_tenant_responde_404_sin_datos(self):
        cambio = self.crear()
        status, body = llamar("GET", "/cambios/{id}", tenant="globex", id=cambio["cambio_id"])
        self.assertEqual(status, 404)
        self.assertNotIn("data", body)
        # Tampoco puede modificarlo
        self.assertEqual(llamar("PUT", "/cambios/{id}", tenant="globex", id=cambio["cambio_id"], body={"titulo": "x"})[0], 404)

    def test_tenant_del_body_se_ignora(self):
        status, body = llamar("POST", "/cambios", body={"titulo": "Cambio", "tenant_id": "globex"})
        self.assertEqual(status, 201)
        self.assertEqual(body["data"]["tenant_id"], "acme")

    def test_referencias_de_otro_tenant_se_rechazan(self):
        status, body = llamar("POST", "/cambios", body={"titulo": "x", "problema_id": "prb-globex-000001"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["codigo"], "PROBLEMA_ID_INVALIDO")

    def test_listado_solo_trae_el_tenant_del_token(self):
        self.crear()
        self.crear(tenant="globex")
        _, body = llamar("GET", "/cambios", tenant="globex")
        self.assertEqual({i["tenant_id"] for i in body["data"]}, {"globex"})


class FlujoTest(BaseTest):
    def test_flujo_completo_hasta_implementado(self):
        cambio = self.crear()
        cid = cambio["cambio_id"]
        self.assertTrue(cid.startswith("chg-acme-"))
        self.assertEqual(cambio["estado"], "BORRADOR")
        self.assertEqual(cambio["solicitante_id"], "usr-acme-00001")
        self.assertNotIn("ventana_inicio", cambio)

        status, body = llamar("POST", "/cambios/{id}/enviar", id=cid, rol="AGENTE", usuario="usr-acme-00005")
        self.assertEqual((status, body["data"]["estado"]), (200, "PENDIENTE_APROBACION"))

        status, body = llamar("POST", "/cambios/{id}/aprobar", id=cid)
        self.assertEqual((status, body["data"]["estado"]), (200, "APROBADO"))
        self.assertEqual(body["data"]["aprobador_id"], "usr-acme-00001")

        status, body = llamar(
            "POST",
            "/cambios/{id}/programar",
            id=cid,
            body={"ventana_inicio": "2026-11-20T02:00:00Z", "ventana_fin": "2026-11-20T04:00:00Z"},
        )
        self.assertEqual((status, body["data"]["estado"]), (200, "PROGRAMADO"))
        self.assertEqual(body["data"]["estado_creado"], f"PROGRAMADO#{cambio['creado_en']}")

        status, body = llamar("POST", "/cambios/{id}/implementar", id=cid, rol="AGENTE", usuario="usr-acme-00005")
        self.assertEqual((status, body["data"]["estado"]), (200, "IMPLEMENTADO"))
        self.assertEqual(body["data"]["implementado_por"], "usr-acme-00005")

    def test_rechazo_exige_motivo(self):
        cid = self.crear()["cambio_id"]
        llamar("POST", "/cambios/{id}/enviar", id=cid)
        self.assertEqual(llamar("POST", "/cambios/{id}/rechazar", id=cid, body={})[0], 400)
        status, body = llamar("POST", "/cambios/{id}/rechazar", id=cid, body={"motivo": "Sin rollback probado"})
        self.assertEqual((status, body["data"]["estado"]), (200, "RECHAZADO"))
        self.assertEqual(body["data"]["motivo_rechazo"], "Sin rollback probado")

    def test_transiciones_invalidas_responden_409(self):
        cid = self.crear()["cambio_id"]
        self.assertEqual(llamar("POST", "/cambios/{id}/aprobar", id=cid)[0], 409)
        self.assertEqual(llamar("POST", "/cambios/{id}/implementar", id=cid)[0], 409)
        status, body = llamar(
            "POST", "/cambios/{id}/programar", id=cid,
            body={"ventana_inicio": "2026-11-20T02:00:00Z", "ventana_fin": "2026-11-20T04:00:00Z"},
        )
        self.assertEqual((status, body["error"]["codigo"]), (409, "TRANSICION_INVALIDA"))

    def test_enviar_exige_planes(self):
        status, body = llamar("POST", "/cambios", body={"titulo": "Sin planes"})
        cid = body["data"]["cambio_id"]
        status, body = llamar("POST", "/cambios/{id}/enviar", id=cid)
        self.assertEqual((status, body["error"]["codigo"]), (400, "PLAN_INCOMPLETO"))

    def test_programar_valida_ventana(self):
        cid = self.crear()["cambio_id"]
        llamar("POST", "/cambios/{id}/enviar", id=cid)
        llamar("POST", "/cambios/{id}/aprobar", id=cid)
        malo = {"ventana_inicio": "2026-11-20T04:00:00Z", "ventana_fin": "2026-11-20T02:00:00Z"}
        self.assertEqual(llamar("POST", "/cambios/{id}/programar", id=cid, body=malo)[0], 400)
        self.assertEqual(llamar("POST", "/cambios/{id}/programar", id=cid, body={"ventana_inicio": "ayer"})[0], 400)

    def test_editar_solo_en_borrador_o_pendiente(self):
        cid = self.crear()["cambio_id"]
        status, body = llamar("PUT", "/cambios/{id}", id=cid, body={"riesgo": "BAJO", "titulo": " Nuevo "})
        self.assertEqual((status, body["data"]["riesgo"], body["data"]["titulo"]), (200, "BAJO", "Nuevo"))
        self.assertEqual(llamar("PUT", "/cambios/{id}", id=cid, body={"estado": "APROBADO"})[0], 400)
        llamar("POST", "/cambios/{id}/enviar", id=cid)
        self.assertEqual(llamar("PUT", "/cambios/{id}", id=cid, body={"riesgo": "ALTO"})[0], 200)
        llamar("POST", "/cambios/{id}/aprobar", id=cid)
        status, body = llamar("PUT", "/cambios/{id}", id=cid, body={"riesgo": "BAJO"})
        self.assertEqual((status, body["error"]["codigo"]), (409, "NO_EDITABLE"))

    def test_validaciones_de_creacion(self):
        self.assertEqual(llamar("POST", "/cambios", body={})[0], 400)
        self.assertEqual(llamar("POST", "/cambios", body={"titulo": "x", "tipo": "RARO"})[0], 400)
        resultado = cambios.handler({**evento("POST", "/cambios"), "body": "{no json"}, None)
        self.assertEqual(resultado["statusCode"], 400)

    def test_ruta_desconocida_404(self):
        self.assertEqual(llamar("DELETE", "/cambios/{id}", id="chg-acme-000001")[0], 404)


class ListadoTest(BaseTest):
    def setUp(self):
        super().setUp()
        tabla = modelo.tabla()
        with tabla.batch_writer() as writer:
            for tenant in ("acme", "globex"):
                for item in seed_cambios.items_tenant(tenant, 60, "test"):
                    writer.put_item(Item=item)

    def test_paginacion_con_limite_y_siguiente(self):
        status, p1 = llamar("GET", "/cambios", query={"limite": "25"})
        self.assertEqual(status, 200)
        self.assertEqual(len(p1["data"]), 25)
        self.assertEqual(p1["paginacion"]["limite"], 25)
        self.assertIsNotNone(p1["paginacion"]["siguiente"])
        _, p2 = llamar("GET", "/cambios", query={"limite": "25", "siguiente": p1["paginacion"]["siguiente"]})
        _, p3 = llamar("GET", "/cambios", query={"limite": "25", "siguiente": p2["paginacion"]["siguiente"]})
        ids = [i["cambio_id"] for i in p1["data"] + p2["data"] + p3["data"]]
        self.assertEqual(len(ids), 60)
        self.assertEqual(len(set(ids)), 60)
        self.assertIsNone(p3["paginacion"]["siguiente"])

    def test_limite_por_defecto_y_maximo(self):
        _, body = llamar("GET", "/cambios")
        self.assertEqual(body["paginacion"]["limite"], 20)
        self.assertEqual(llamar("GET", "/cambios", query={"limite": "101"})[0], 400)
        self.assertEqual(llamar("GET", "/cambios", query={"limite": "abc"})[0], 400)

    def test_cursor_de_otro_tenant_se_rechaza(self):
        _, p1 = llamar("GET", "/cambios", tenant="globex", query={"limite": "5"})
        status, body = llamar("GET", "/cambios", query={"siguiente": p1["paginacion"]["siguiente"]})
        self.assertEqual((status, body["error"]["codigo"]), (400, "CURSOR_INVALIDO"))

    def test_filtro_por_estado_usa_gsi1(self):
        _, body = llamar("GET", "/cambios", query={"estado": "IMPLEMENTADO", "limite": "100"})
        self.assertTrue(body["data"])
        self.assertEqual({i["estado"] for i in body["data"]}, {"IMPLEMENTADO"})
        self.assertEqual(llamar("GET", "/cambios", query={"estado": "CUALQUIERA"})[0], 400)

    def test_filtro_por_tipo_y_riesgo(self):
        _, body = llamar("GET", "/cambios", query={"tipo": "NORMAL", "riesgo": "ALTO", "limite": "100"})
        self.assertTrue(all(i["tipo"] == "NORMAL" and i["riesgo"] == "ALTO" for i in body["data"]))

    def test_calendario_por_rango_de_ventana(self):
        status, body = llamar("GET", "/cambios/calendario", query={"desde": "2025-01-01", "hasta": "2027-12-31", "limite": "100"})
        self.assertEqual(status, 200)
        self.assertTrue(body["data"])
        ventanas = [i["ventana_inicio"] for i in body["data"]]
        self.assertEqual(ventanas, sorted(ventanas))
        self.assertTrue({i["estado"] for i in body["data"]} <= {"PROGRAMADO", "IMPLEMENTADO"})
        self.assertEqual({i["tenant_id"] for i in body["data"]}, {"acme"})

    def test_calendario_valida_parametros(self):
        self.assertEqual(llamar("GET", "/cambios/calendario")[0], 400)
        self.assertEqual(llamar("GET", "/cambios/calendario", query={"desde": "2026-12-01", "hasta": "2026-11-01"})[0], 400)


class EventosTest(BaseTest):
    def test_publicador_es_no_op_con_eventos_desactivados(self):
        from src.common import events

        self.assertIsNone(events.publicar("cambio.implementado", "acme", {"cambio_id": "x"}))

    def test_consumidor_crea_rfc_desde_causa_raiz_de_forma_idempotente(self):
        sobre = {
            "evento": "problema.causa_raiz_identificada",
            "tenant_id": "acme",
            "data": {"problema_id": "prb-acme-000123", "causa_raiz": "Memoria insuficiente"},
        }
        registro = {"messageId": "m1", "body": json.dumps(sobre)}
        self.assertEqual(eventos.handler({"Records": [registro, {**registro, "messageId": "m2"}]}, None)["batchItemFailures"], [])
        _, body = llamar("GET", "/cambios")
        self.assertEqual(len(body["data"]), 1)
        self.assertEqual(body["data"][0]["estado"], "BORRADOR")
        self.assertEqual(body["data"][0]["problema_id"], "prb-acme-000123")

    def test_consumidor_crea_cambio_estandar_aprobado(self):
        sobre = {"evento": "solicitud.cambio_estandar", "tenant_id": "acme", "data": {"solicitud_id": "req-acme-000001"}}
        sns = {"Type": "Notification", "Message": json.dumps(sobre)}
        eventos.handler({"Records": [{"messageId": "m1", "body": json.dumps(sns)}]}, None)
        _, body = llamar("GET", "/cambios", query={"estado": "APROBADO"})
        self.assertEqual(body["data"][0]["tipo"], "ESTANDAR")


class SeedTest(unittest.TestCase):
    def test_seed_reproducible_y_con_ids_del_contrato(self):
        a = list(seed_cambios.items_tenant("acme", 50, "2026"))
        b = list(seed_cambios.items_tenant("acme", 50, "2026"))
        self.assertEqual(a, b)
        self.assertEqual(a[0]["cambio_id"], "chg-acme-000001")
        self.assertEqual(a[-1]["cambio_id"], "chg-acme-000050")
        for item in a:
            self.assertTrue(item["solicitante_id"].startswith("usr-acme-"))
            self.assertTrue(item["estado_creado"].startswith(item["estado"] + "#"))
            if item["problema_id"]:
                self.assertTrue(item["problema_id"].startswith("prb-acme-"))
            if item["estado"] in ("PROGRAMADO", "IMPLEMENTADO"):
                self.assertLess(item["ventana_inicio"], item["ventana_fin"])
            else:
                self.assertNotIn("ventana_inicio", item)


if __name__ == "__main__":
    unittest.main()
