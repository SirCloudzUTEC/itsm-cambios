# itsm-cambios

Microservicio de **Gestión de Cambios (RFC)** del SaaS ITSM: registro de la solicitud de cambio, evaluación de riesgo, aprobación, programación en ventana e implementación. Es multi-tenant, serverless (API Gateway + Lambda + DynamoDB) y se protege con el Lambda Authorizer compartido de `itsm-auth`.

> **Estado actual:** desplegado en AWS (`dev`, `us-east-1`) con API Gateway, Lambda Python 3.12, DynamoDB y eventos SNS/SQS habilitados. Publica eventos mediante `itsm-eventos-dev` y consume `itsm-q-cambios-dev`.

## Parámetros comunes

| Parámetro | Valor |
|---|---|
| Región / stage | `us-east-1` / `dev` |
| Runtime | Python 3.12 |
| Framework | Serverless Framework 3.x |
| Rol | `LabRole` existente de AWS Academy |
| Authorizer | `${ssm:/itsm/dev/authorizer-arn}` (token, `resultTtlInSeconds: 0`) |
| Tabla | `itsm-cambios-dev-cambios` (PK `tenant_id`, SK `cambio_id`) |

## Estructura

```text
serverless.yml               API, tabla DynamoDB (GSI1 estado, GSI2 ventana) y authorizer compartido
src/handlers/cambios.py      endpoints y flujo de estados
src/common/                  http (respuestas), auth (contexto del authorizer), modelo (DynamoDB), events (publicacion SNS)
src/consumers/eventos.py     consumidor de q-cambios (desplegado y habilitado)
seed/seed_cambios.py         10 000 cambios por tenant con IDs deterministas
scripts/contar_por_tenant.py conteo por tenant para la evidencia
scripts/generate_dev_token.py JWT de prueba (requiere JWT_SECRET; no compartirlo)
docs/modelo.json             estructura JSON de la tabla para el informe
tests/                       26 pruebas con DynamoDB simulado (moto)
```

## Endpoints

Todos requieren `Authorization: Bearer <JWT>` y el rol `ADMIN` o `AGENTE` (con `SOLICITANTE` la respuesta es 403). El tenant **siempre** sale del contexto del authorizer; si el body trae `tenant_id`, se ignora.

| Método | Ruta | Acceso | Uso |
|---|---|---|---|
| POST | `/cambios` | ADMIN, AGENTE | Crea un RFC en `BORRADOR` (`titulo`, `descripcion`, `tipo`, `riesgo`, `plan_implementacion`, `plan_rollback`, `problema_id?`, `solicitud_id?`) |
| GET | `/cambios?limite=20&siguiente=…&estado=…&tipo=…&riesgo=…` | ADMIN, AGENTE | Lista paginada (Query, sin Scan) |
| GET | `/cambios/{id}` | ADMIN, AGENTE | Detalle; un ID de otro tenant responde 404 |
| PUT | `/cambios/{id}` | ADMIN, AGENTE | Edita solo en `BORRADOR` o `PENDIENTE_APROBACION` |
| POST | `/cambios/{id}/enviar` | ADMIN, AGENTE | `BORRADOR → PENDIENTE_APROBACION` (exige ambos planes) |
| POST | `/cambios/{id}/aprobar` | **ADMIN** | `PENDIENTE_APROBACION → APROBADO` |
| POST | `/cambios/{id}/rechazar` | **ADMIN** | `PENDIENTE_APROBACION → RECHAZADO`; body `{"motivo": "..."}` |
| POST | `/cambios/{id}/programar` | ADMIN, AGENTE | `APROBADO/PROGRAMADO → PROGRAMADO`; body `{"ventana_inicio", "ventana_fin"}` en ISO 8601 UTC |
| POST | `/cambios/{id}/implementar` | ADMIN, AGENTE | `PROGRAMADO → IMPLEMENTADO`; body opcional `{"notas_implementacion"}` |
| GET | `/cambios/calendario?desde=2026-11-01&hasta=2026-11-30` | ADMIN, AGENTE | Cambios con ventana en el rango (GSI2), paginado |

Las respuestas siguen el contrato:
- Éxito: `{"data": ..., "paginacion": {"limite": 20, "siguiente": "token|null"}}`
- Error: `{"error": {"codigo": "...", "mensaje": "..."}}`
- Códigos HTTP: 200, 201, 400, 401, 403, 404, 409 (transición inválida o conflicto) y 500.

```text
BORRADOR ─enviar→ PENDIENTE_APROBACION ─aprobar→ APROBADO ─programar→ PROGRAMADO ─implementar→ IMPLEMENTADO
                                     └─rechazar→ RECHAZADO           (programar de nuevo = reprogramar)
```

`CERRADO`, que aparecía en el plan original, no se incluye porque el contrato no define un endpoint para llegar a ese estado. La misma regla se aplicó a Solicitudes.

## Despliegue

Requisitos: Node.js 20, `npm install -g serverless@3`, Python 3.12, AWS CLI con las credenciales del Learner Lab.

```bash
aws sts get-caller-identity                     # confirmar la misma cuenta que el resto del grupo
aws ssm get-parameter --name /itsm/dev/authorizer-arn --region us-east-1   # debe existir (itsm-auth)
serverless deploy --stage dev                   # guardar la salida en docs/ como evidencia (sin secretos)
```

La salida muestra `CambiosApiUrl`. Esa URL base, sin `/cambios`, va en `VITE_API_CAMBIOS` del frontend y en el canal del grupo.

**No ejecutes `serverless remove`.** La tabla tiene `DeletionPolicy: Retain` para no perder los datos, pero el contrato prohíbe ese comando igualmente.

## Datos ficticios (30 000 registros)

```bash
python -m pip install -r requirements-seed.txt
python seed/seed_cambios.py --tenant acme --dry-run     # vista previa, no escribe
python seed/seed_cambios.py --tenant acme               # hito 14-Nov: 10 000 en un tenant
python seed/seed_cambios.py --tenant todos              # hito final: 10 000 × acme, globex, initech
python scripts/contar_por_tenant.py                     # evidencia del conteo por tenant
```

Cómo se generan los datos:
- Los IDs son `chg-{tenant}-{n:06d}`.
- La fecha base es fija (2026-11-10) y la semilla también, así que el seed es reproducible y se puede repetir: sobrescribe los mismos IDs.
- Las referencias son coherentes con los demás seeds:
  - `solicitante_id` va de `usr-{t}-00002` a `00101`, que son AGENTE en itsm-auth.
  - `aprobador_id` es `usr-{t}-00001`, el ADMIN.
  - `problema_id` usa `prb-{t}-NNNNNN` y `solicitud_id` usa `req-{t}-NNNNNN`.
- Distribución aproximada de estados: 55% IMPLEMENTADO, 10% PROGRAMADO (con ventanas en los próximos 60 días, para que el calendario muestre datos), 10% PENDIENTE, 9% BORRADOR, 8% APROBADO y 8% RECHAZADO.

## Pruebas

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

Las pruebas no necesitan AWS. Cubren:
- 401 sin contexto y 403 por rol.
- 404 al pedir un ID de otro tenant, y que el `tenant_id` del body se ignora.
- Validación de referencias de otro tenant.
- El flujo completo de estados y los 409 por transición inválida.
- La paginación con `limite` y `siguiente`, y el rechazo de un cursor de otro tenant.
- Los filtros por GSI1, el calendario por GSI2, el publicador en modo deshabilitado, el consumidor idempotente y la reproducibilidad del seed.

## Pruebas de integración (evidencias para el ACL y el informe)

Obtén un token con `POST {VITE_API_AUTH}/auth/login` y luego:

| Prueba | Comando | Esperado |
|---|---|---|
| Sin token | `curl -i $API/cambios` | 401 |
| Token válido | `curl -i -H "Authorization: Bearer $TOKEN_ACME_ADMIN" $API/cambios?limite=5` | 200 con `paginacion.siguiente` |
| Rol insuficiente | `curl -i -X POST -H "Authorization: Bearer $TOKEN_ACME_AGENTE" $API/cambios/chg-acme-000002/aprobar` | 403 |
| Aislamiento | `curl -i -H "Authorization: Bearer $TOKEN_GLOBEX" $API/cambios/chg-acme-000001` | 404 |
| Paginación | repetir con `&siguiente=<token>` | página siguiente sin repetir IDs |
| Conteo | `python scripts/contar_por_tenant.py` | 10 000 por tenant |
| Logs | CloudWatch → `/aws/lambda/itsm-cambios-dev-api` | líneas `accion {...}` con tenant, usuario y cambio |

## Integracion por eventos desplegada

La infraestructura compartida de `itsm-infra` proporciona el topico SNS `itsm-eventos-dev`, las colas SQS, sus DLQ y los filtros de suscripcion.

La configuracion desplegada de Cambios utiliza:

- `EVENTS_ENABLED=true`.
- `SNS_EVENTOS_ARN` obtenido de SSM Parameter Store.
- Lambda `itsm-cambios-dev-consumirQCambios` conectada a `itsm-q-cambios-dev`.
- Procesamiento SQS habilitado con `ReportBatchItemFailures`.

### Eventos publicados

- `cambio.aprobado`: registra la aprobacion de un RFC.
- `cambio.implementado`: contiene `cambio_id`, `problema_id` y `solicitud_id`. Los consumidores respectivos utilizan la referencia que corresponda.

### Eventos consumidos

- `problema.causa_raiz_identificada`: genera un RFC normal en `BORRADOR` vinculado al problema.
- `solicitud.cambio_estandar`: genera un RFC estandar preaprobado, con estado `APROBADO`, vinculado a la solicitud.

Los consumidores usan identificadores deterministas para evitar duplicados cuando SQS vuelve a entregar un evento.

### Integraciones verificadas en AWS

**Problemas -> Cambios -> Problemas -> Incidentes**

- Problema: `prb-acme-evt856d7948b6b3`.
- RFC generado: `chg-acme-e40688e8e5ca`.
- RFC implementado y problema resuelto automaticamente.
- Incidente vinculado `inc-acme-010001` resuelto mediante `problema.resuelto`.

**Solicitudes -> Cambios -> Solicitudes**

- Solicitud: `req-acme-1795327666595-ppso`.
- RFC generado: `chg-acme-e1c48fc7f10a`.
- Tipo `ESTANDAR`, riesgo `BAJO`, estado inicial `APROBADO`.
- Tras implementar el RFC, la solicitud paso automaticamente a `COMPLETADA` y registro su `cambio_id`.

Ambos circuitos se verificaron mediante las APIs desplegadas y consultas a DynamoDB.

### Limitacion conocida

La API exige que el RFC este `PROGRAMADO` antes de implementarlo, pero no comprueba que la hora actual este dentro de la ventana programada. La implementacion anticipada fue aceptada durante una prueba. Esta validacion temporal queda pendiente de mejora.
