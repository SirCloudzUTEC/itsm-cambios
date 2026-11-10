"""Evidencia para el informe: conteo de cambios por tenant (Query + Select=COUNT, sin Scan)."""
import argparse
import os

import boto3
from boto3.dynamodb.conditions import Key

TENANTS = ("acme", "globex", "initech")


def contar(tabla, tenant_id):
    total = 0
    params = {"KeyConditionExpression": Key("tenant_id").eq(tenant_id), "Select": "COUNT"}
    while True:
        resultado = tabla.query(**params)
        total += resultado["Count"]
        if "LastEvaluatedKey" not in resultado:
            return total
        params["ExclusiveStartKey"] = resultado["LastEvaluatedKey"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", default=os.environ.get("CAMBIOS_TABLE", "itsm-cambios-dev-cambios"))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    args = parser.parse_args()
    tabla = boto3.resource("dynamodb", region_name=args.region).Table(args.table)
    total = 0
    print(f"Tabla: {args.table}")
    for tenant_id in TENANTS:
        cantidad = contar(tabla, tenant_id)
        total += cantidad
        print(f"  {tenant_id:<8} {cantidad:>6}")
    print(f"  {'TOTAL':<8} {total:>6}")


if __name__ == "__main__":
    main()
