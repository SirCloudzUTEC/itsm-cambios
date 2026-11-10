"""Seed reproducible de cambios: 10 000 por tenant (30 000 en total).

Uso:
    python seed/seed_cambios.py --tenant acme                 # hito 14-Nov (1 tenant)
    python seed/seed_cambios.py --tenant todos                # hito final (3 tenants)
    python seed/seed_cambios.py --tenant acme --dry-run       # muestra 3 ejemplos sin AWS

IDs deterministas (contrato v1.1): chg-{tenant}-{n:06d}. Referencias del mismo tenant:
  solicitante_id -> usr-{tenant}-00002..00101 (AGENTE en el seed de itsm-auth)
  aprobador_id   -> usr-{tenant}-00001        (ADMIN en el seed de itsm-auth)
  problema_id    -> prb-{tenant}-{n:06d}      (seed de itsm-problemas)
  solicitud_id   -> req-{tenant}-{n:06d}      (seed de itsm-solicitudes)
"""
import argparse
import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone

TENANTS = ("acme", "globex", "initech")
POR_TENANT = 10_000
FECHA_BASE = datetime(2026, 11, 10, tzinfo=timezone.utc)  # fija => datos reproducibles

ESTADOS = (  # distribución realista: la mayoría ya implementados
    ("IMPLEMENTADO", 55),
    ("PROGRAMADO", 10),
    ("APROBADO", 8),
    ("PENDIENTE_APROBACION", 10),
    ("BORRADOR", 9),
    ("RECHAZADO", 8),
)
TIPOS = (("NORMAL", 60), ("ESTANDAR", 30), ("EMERGENCIA", 10))
RIESGOS = {"ESTANDAR": ("BAJO",), "NORMAL": ("BAJO", "MEDIO", "ALTO"), "EMERGENCIA": ("MEDIO", "ALTO")}
CATALOGO = (
    ("Actualizar firmware del firewall perimetral", "Aplicar parche de seguridad del fabricante.", "Restaurar firmware previo desde backup del equipo."),
    ("Ampliar almacenamiento del servidor de archivos", "Agregar 2 TB al volumen compartido.", "Revertir snapshot del volumen."),
    ("Migrar base de datos de RR.HH. a nueva versión", "Upgrade de motor y prueba de integridad.", "Restaurar backup completo previo a la migración."),
    ("Renovar certificado TLS del portal", "Instalar certificado nuevo y reiniciar balanceador.", "Reinstalar el certificado anterior."),
    ("Aplicar parches mensuales de Windows Server", "Instalar parches en ventana nocturna por lotes.", "Desinstalar KB problemáticos y reiniciar."),
    ("Cambiar reglas de VPN para sede remota", "Agregar subred y políticas en el concentrador.", "Eliminar reglas nuevas y restaurar configuración."),
    ("Actualizar cliente de correo corporativo", "Distribuir versión nueva mediante MDM.", "Volver a publicar la versión anterior en MDM."),
    ("Reconfigurar DNS interno", "Actualizar zonas y TTL de registros críticos.", "Restaurar archivo de zona respaldado."),
    ("Escalar cluster de aplicaciones", "Agregar dos nodos al grupo de autoescalado.", "Reducir capacidad al valor anterior."),
    ("Rotar credenciales de cuentas de servicio", "Generar secretos nuevos y actualizar aplicaciones.", "Reactivar las credenciales anteriores temporalmente."),
)
MOTIVOS_RECHAZO = (
    "Plan de rollback insuficiente.",
    "Conflicto con otra ventana de mantenimiento.",
    "Riesgo no justificado para el beneficio esperado.",
)


def _elegir(rnd, opciones):
    return rnd.choices([o for o, _ in opciones], weights=[p for _, p in opciones])[0]


def _iso(fecha):
    return fecha.strftime("%Y-%m-%dT%H:%M:%SZ")


def generar(tenant_id, n, rnd):
    titulo, plan, rollback = rnd.choice(CATALOGO)
    tipo = _elegir(rnd, TIPOS)
    estado = _elegir(rnd, ESTADOS)
    # Un IMPLEMENTADO se creó al menos 15 días antes para que su ventana quede en el pasado.
    dias_min = 15 if estado == "IMPLEMENTADO" else 0
    creado = FECHA_BASE - timedelta(
        days=rnd.randint(dias_min, 364), hours=rnd.randint(0, 23), minutes=rnd.randint(0, 59)
    )
    item = {
        "tenant_id": tenant_id,
        "cambio_id": f"chg-{tenant_id}-{n:06d}",
        "titulo": titulo,
        "descripcion": f"{titulo}. Registro de prueba #{n}.",
        "tipo": tipo,
        "riesgo": rnd.choice(RIESGOS[tipo]),
        "estado": estado,
        "estado_creado": f"{estado}#{_iso(creado)}",
        "plan_implementacion": plan if estado != "BORRADOR" or rnd.random() < 0.5 else "",
        "plan_rollback": rollback if estado != "BORRADOR" or rnd.random() < 0.5 else "",
        "solicitante_id": f"usr-{tenant_id}-{rnd.randint(2, 101):05d}",
        "aprobador_id": None,
        "motivo_rechazo": None,
        # ~30% nace de un problema; los ESTANDAR suelen venir de una solicitud de catálogo.
        "problema_id": f"prb-{tenant_id}-{rnd.randint(1, POR_TENANT):06d}" if rnd.random() < 0.3 else None,
        "solicitud_id": (
            f"req-{tenant_id}-{rnd.randint(1, POR_TENANT):06d}" if tipo == "ESTANDAR" and rnd.random() < 0.6 else None
        ),
        "creado_en": _iso(creado),
        "actualizado_en": _iso(creado),
    }
    if estado in ("APROBADO", "PROGRAMADO", "IMPLEMENTADO", "RECHAZADO"):
        item["aprobador_id"] = f"usr-{tenant_id}-00001"
        item["actualizado_en"] = _iso(creado + timedelta(days=1))
    if estado == "APROBADO":
        item["aprobado_en"] = item["actualizado_en"]
    if estado == "RECHAZADO":
        item["motivo_rechazo"] = rnd.choice(MOTIVOS_RECHAZO)
    if estado in ("PROGRAMADO", "IMPLEMENTADO"):
        if estado == "PROGRAMADO":  # ventanas futuras: el calendario muestra próximos 60 días
            inicio = FECHA_BASE + timedelta(days=rnd.randint(1, 60))
        else:
            inicio = creado + timedelta(days=rnd.randint(2, 14))
        inicio = inicio.replace(hour=rnd.choice((0, 1, 2, 22, 23)), minute=0, second=0)
        item["ventana_inicio"] = _iso(inicio)
        item["ventana_fin"] = _iso(inicio + timedelta(hours=rnd.choice((1, 2, 4))))
        if estado == "IMPLEMENTADO":
            item["implementado_en"] = item["ventana_fin"]
            item["implementado_por"] = item["solicitante_id"]
            item["notas_implementacion"] = "Implementado sin incidencias."
            item["actualizado_en"] = item["ventana_fin"]
    return item


def items_tenant(tenant_id, cantidad, semilla):
    rnd = random.Random(f"{semilla}-{tenant_id}")
    for n in range(1, cantidad + 1):
        yield generar(tenant_id, n, rnd)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--table", default=os.environ.get("CAMBIOS_TABLE", "itsm-cambios-dev-cambios"))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--tenant", choices=TENANTS + ("todos",), default="todos")
    parser.add_argument("--count", type=int, default=POR_TENANT, help="registros por tenant (1..10000)")
    parser.add_argument("--seed", default="2026")
    parser.add_argument("--dry-run", action="store_true", help="no escribe en AWS; imprime ejemplos")
    args = parser.parse_args()
    if not 1 <= args.count <= POR_TENANT:
        parser.error("--count debe estar entre 1 y 10000")

    tenants = TENANTS if args.tenant == "todos" else (args.tenant,)
    if args.dry_run:
        for tenant_id in tenants:
            muestra = list(items_tenant(tenant_id, min(args.count, 3), args.seed))
            print(json.dumps(muestra, indent=2, ensure_ascii=False))
        return 0

    import boto3

    tabla = boto3.resource("dynamodb", region_name=args.region).Table(args.table)
    for tenant_id in tenants:
        with tabla.batch_writer(overwrite_by_pkeys=["tenant_id", "cambio_id"]) as writer:
            for item in items_tenant(tenant_id, args.count, args.seed):
                writer.put_item(Item=item)
                n = int(item["cambio_id"].rsplit("-", 1)[1])
                if n % 1000 == 0 or n == args.count:
                    print(f"{tenant_id}: {n}/{args.count}", flush=True)
    print("Listo. Verifica con: python scripts/contar_por_tenant.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
