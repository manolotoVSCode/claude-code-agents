#!/usr/bin/env python3
"""Guardián de SQL.

Hook PreToolUse para las herramientas MCP que ejecutan SQL (por ejemplo,
execute_sql o apply_migration). Clasifica la sentencia y decide:

  VERDE  solo lectura, o ensayo envuelto en BEGIN ... ROLLBACK  -> pasa
  ÁMBAR  escritura recuperable                                  -> exige aprobación
         del agente guardian (.claude/guardian/aprobaciones/<sha256>.json)
  ROJO   destructiva o de permisos                              -> exige además
         "aprobacion_humana" en ese archivo

Cada aprobación es de un solo uso y está ligada al hash exacto del SQL: si el
SQL cambia una coma, hace falta otra revisión. Todo queda en registro.log.
Ante cualquier error interno, deniega (falla cerrada).

La clasificación es heurística, con expresiones regulares y un lexer mínimo.
Es un freno, no un entorno aislado: ver docs/LIMITACIONES.md.

Configuración por variables de entorno:
  GUARDIAN_ESQUEMAS_PROTEGIDOS  esquemas cuya mención en una escritura la hace
                                ROJA, separados por comas (por defecto,
                                pg_catalog,information_schema).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime

RAIZ = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
DIR_G = os.path.join(RAIZ, ".claude", "guardian")
DIR_APROB = os.path.join(DIR_G, "aprobaciones")
REGISTRO = os.path.join(DIR_G, "registro.log")

ESQUEMAS = [e.strip().lower() for e in
            os.environ.get("GUARDIAN_ESQUEMAS_PROTEGIDOS", "pg_catalog,information_schema").split(",")
            if e.strip()]

SOLO_LECTURA = re.compile(r"^(select|with|explain|show|values|table)\b")
ESCRITURA = re.compile(
    r"\b(insert|update|delete|merge|create|alter|drop|truncate|grant|revoke|"
    r"comment|copy|vacuum|reindex|cluster|refresh|call|do|lock|security)\b"
)
# En un ensayo BEGIN…ROLLBACK, cosas cuyo efecto no deshace el ROLLBACK
# (archivos del servidor, sesión, secuencias, cron, otros procesos).
NO_TRANSACCIONAL = re.compile(
    r"\b(commit|end|vacuum|concurrently|alter\s+system|create\s+database|"
    r"drop\s+database|dblink\w*|pg_terminate_backend|pg_cancel_backend|"
    r"copy|lo_\w+|pg_advisory\w*|pg_read\w*|pg_ls_\w+|pg_reload_conf|pg_rotate_logfile|"
    r"set_config|setval|nextval|http\w*|net\.\w+|cron\.\w+|pg_notify|discard|reset|set\s+session|set\s+role)\b"
)
CONTROL_TX = re.compile(r"^(begin|start|commit|end|rollback|abort|prepare|savepoint|release)\b")
ROJO = [
    (r"\bdrop\s+(table|schema|database|view|materialized|function|procedure|type|policy|trigger|column|index|extension)\b", "DROP"),
    (r"\balter\s+table\b[^;]*\bdrop\b", "ALTER TABLE ... DROP"),
    (r"\btruncate\b", "TRUNCATE"),
    (r"\b(grant|revoke)\b", "cambio de permisos"),
    (r"\b(create|alter|drop)\s+(role|user)\b", "cambio de roles"),
    (r"\bdisable\s+row\s+level\s+security\b", "desactiva RLS"),
    (r"\bsession_replication_role\b|\bdisable\s+trigger\b", "desactiva triggers"),
]
if ESQUEMAS:
    ROJO.append((r"\b(" + "|".join(re.escape(e) for e in ESQUEMAS) + r")\.\w+", "esquema protegido"))


def lexer(sql: str) -> tuple[str, bool]:
    """Una sola pasada de izquierda a derecha, como el lexer de PostgreSQL.

    Devuelve (sql normalizado, opaco). Los literales '…' se reducen a '' y los
    comentarios -- se quitan, en el orden en que aparecen (un -- dentro de un
    literal es texto, no comentario). Todo lo que este lexer no modela con
    certeza marca el SQL como opaco y le quita cualquier vía VERDE: comentarios
    de bloque /* */ (anidables en PostgreSQL), identificadores entre comillas
    dobles, literales con dólar ($$, $tag$), cadenas E'…' con escapes y
    literales sin cerrar.
    """
    salida, i, n = [], 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "'":
            previo = sql[i - 1] if i else " "
            if previo in "eE" and (i < 2 or not (sql[i - 2].isalnum() or sql[i - 2] == "_")):
                return "", True  # E'…' admite \' : no se modela
            j = i + 1
            while True:
                k = sql.find("'", j)
                if k == -1:
                    return "", True  # literal sin cerrar
                if k + 1 < n and sql[k + 1] == "'":
                    j = k + 2
                    continue
                break
            salida.append("''")
            i = k + 1
            continue
        if c == "-" and sql.startswith("--", i):
            k = sql.find("\n", i)
            i = n if k == -1 else k
            salida.append(" ")
            continue
        if c == "/" and sql.startswith("/*", i):
            return "", True
        if c in '"$':
            return "", True
        salida.append(c)
        i += 1
    return re.sub(r"\s+", " ", "".join(salida)).strip().lower(), False


def sentencias(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip()]


_DML = re.compile(
    r"\bdelete\s+from\b|\bupdate\s+(?:only\s+)?[a-z_][\w.]*(?:\s+(?:as\s+)?[a-z_]\w*)?\s+set\b"
)


def sin_where(stmts: list[str]) -> str | None:
    """Busca un DELETE o UPDATE sin WHERE en su propio nivel de paréntesis.

    Así se detecta también el que va dentro de un WITH, y no cuenta como
    filtro un WHERE que solo está en una subconsulta.
    """
    for s in stmts:
        prof = []  # profundidad de paréntesis en cada posición
        d = 0
        for c in s:
            if c == "(":
                d += 1
            prof.append(d)
            if c == ")":
                d = max(d - 1, 0)
        for m in _DML.finditer(s):
            nivel = prof[m.start()]
            fin = len(s)
            for k in range(m.end(), len(s)):
                if prof[k] < nivel or (s[k] == ")" and prof[k] == nivel):
                    fin = k
                    break
            tramo = s[m.end():fin]
            tiene_where = any(prof[m.end() + w.start()] == nivel
                              for w in re.finditer(r"\bwhere\b", tramo))
            if not tiene_where:
                return "DELETE sin WHERE" if m.group(0).startswith("delete") else "UPDATE sin WHERE"
    return None


# Palabras que pueden ir seguidas de "(" sin ser una llamada a función, y
# funciones de solo lectura sin efectos. Cualquier otro nombre seguido de "("
# (o cualquier función calificada con esquema) se trata como posible efecto:
# una "lectura" que llama funciones desconocidas no es VERDE, porque una
# función puede escribir o actuar fuera de la base.
_PALABRAS = {
    "select", "from", "where", "and", "or", "not", "in", "exists", "any", "all", "some",
    "values", "over", "filter", "within", "as", "join", "on", "using", "having", "by",
    "case", "when", "then", "else", "is", "between", "like", "ilike", "distinct", "with",
    "lateral", "array", "row", "union", "intersect", "except", "limit", "offset",
    "partition", "order", "group", "returning", "numeric", "decimal", "varchar", "char",
    "timestamp", "timestamptz", "interval", "cast", "tablesample",
}
_FUNCIONES_SEGURAS = {
    "count", "sum", "min", "max", "avg", "coalesce", "nullif", "now", "date_trunc",
    "date_part", "extract", "lower", "upper", "trim", "btrim", "length", "char_length",
    "substring", "substr", "string_agg", "array_agg", "array_length", "jsonb_array_length",
    "jsonb_typeof", "json_agg", "jsonb_agg", "jsonb_build_object", "json_build_object",
    "to_jsonb", "to_char", "to_date", "round", "abs", "greatest", "least", "concat",
    "left", "right", "replace", "split_part", "position", "age", "row_number", "rank",
    "dense_rank", "lag", "lead", "first_value", "last_value", "bool_or", "bool_and",
    "format_type", "obj_description", "col_description", "pg_get_constraintdef",
    "pg_get_indexdef", "pg_get_viewdef", "pg_get_functiondef", "pg_get_expr",
    "pg_size_pretty", "pg_total_relation_size", "pg_relation_size", "current_setting",
    "version", "has_table_privilege",
}


def funciones_desconocidas(stmt: str) -> list[str]:
    raras = []
    for nombre in re.findall(r"([a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)?)\s*\(", stmt):
        if "." in nombre and not nombre.startswith("pg_catalog."):
            raras.append(nombre)
            continue
        base = nombre.split(".")[-1]
        if base not in _PALABRAS and base not in _FUNCIONES_SEGURAS:
            raras.append(nombre)
    return raras


def clasificar(sql_original: str) -> tuple[str, str]:
    # SQL opaco (ver lexer): sin vía VERDE; se clasifica con un normalizado
    # aproximado para decidir entre ÁMBAR y ROJO, y decide el guardian.
    sql, opaco = lexer(sql_original)
    if opaco:
        sql = re.sub(r"\s+", " ", sql_original).strip().lower()
    stmts = sentencias(sql)
    if not stmts:
        return ("ambar", "SQL opaco") if opaco else ("verde", "vacío")
    if not opaco:
        # SELECT … INTO crea una tabla: no es una lectura.
        if all(SOLO_LECTURA.match(s) and not ESCRITURA.search(s) and not re.search(r"\binto\b", s)
               for s in stmts):
            raras = [f for s in stmts for f in funciones_desconocidas(s)]
            if not raras:
                return "verde", "solo lectura"
            return "ambar", "lectura que llama funciones no reconocidas: " + ", ".join(sorted(set(raras))[:5])
    if (
        not opaco
        and stmts[0] in ("begin", "begin transaction", "start transaction")
        and stmts[-1] == "rollback"
        and not NO_TRANSACCIONAL.search(" ; ".join(stmts[1:-1]))
        # Ningún control de transacción en medio: un ABORT o COMMIT intermedio
        # dejaría lo que sigue fuera del ROLLBACK final.
        and not any(CONTROL_TX.match(s) for s in stmts[1:-1])
    ):
        return "verde", "ensayo con ROLLBACK"
    motivo = sin_where(stmts)
    if motivo:
        return "rojo", motivo
    for patron, motivo in ROJO:
        if re.search(patron, sql):
            return "rojo", motivo
    return "ambar", "escritura"


def registrar(linea: str) -> None:
    try:  # el registro es auditoría; si falla, la decisión se entrega igual
        os.makedirs(DIR_G, exist_ok=True)
        with open(REGISTRO, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {linea}\n")
    except OSError:
        pass


def decidir(permitir: bool, motivo: str) -> None:
    salida = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow" if permitir else "deny",
            "permissionDecisionReason": motivo,
        }
    }
    print(json.dumps(salida, ensure_ascii=False))
    sys.exit(0)


def main() -> None:
    evento = json.load(sys.stdin)
    herramienta = str(evento.get("tool_name", ""))
    entrada = evento.get("tool_input") or {}
    sql = entrada.get("query")
    if sql is None:
        sql = entrada.get("sql")
    if sql is None:
        # Sin SQL reconocible. Si la herramienta parece ejecutar SQL, no se
        # arriesga; si no (por ejemplo, listar tablas o migraciones), sigue el
        # flujo normal.
        nombre = herramienta.rsplit("__", 1)[-1]
        if re.search(r"(^|_)(execute|apply|run|query)", nombre, re.I):
            decidir(False, f"Guardián: no encontré el SQL en la entrada de {herramienta}; se deniega por seguridad.")
        sys.exit(0)
    sql = str(sql)
    sha = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    nivel, motivo = clasificar(sql)

    if "migration" in herramienta.lower() and nivel == "verde":
        nivel, motivo = "ambar", "migración"

    if nivel == "verde":
        registrar(f"VERDE {sha[:12]} {motivo}")
        sys.exit(0)  # sigue el flujo normal de permisos

    ruta = os.path.join(DIR_APROB, f"{sha}.json")
    if not os.path.exists(ruta):
        registrar(f"BLOQUEADO {nivel.upper()} {sha[:12]} {motivo}")
        instruccion = (
            f"Guardián: SQL {nivel.upper()} ({motivo}), sha256={sha}. "
            "Invoca el agente 'guardian' con este SQL exacto para que lo pruebe "
            "y, si procede, escriba la aprobación."
        )
        if nivel == "rojo":
            instruccion += (
                " Al ser ROJO, además necesita aprobación humana: pídela a una "
                "persona y no lo reintentes hasta tenerla."
            )
        decidir(False, instruccion)

    with open(ruta, encoding="utf-8") as f:
        aprob = json.load(f)
    if not isinstance(aprob, dict):
        raise ValueError("la aprobación no es un objeto JSON")
    if nivel == "rojo" and not str(aprob.get("aprobacion_humana", "")).strip():
        registrar(f"BLOQUEADO ROJO {sha[:12]} {motivo} sin aprobación humana")
        decidir(False, "Guardián: SQL ROJO sin 'aprobacion_humana' en la aprobación.")

    os.replace(ruta, ruta + ".usada")
    registrar(f"PERMITIDO {nivel.upper()} {sha[:12]} {motivo} aprobado_por={aprob.get('revisado_por', '?')}")
    decidir(True, f"Guardián: {nivel.upper()} aprobado ({aprob.get('resumen', '')})")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        decidir(False, f"Guardián: error interno ({exc!r}); se deniega por seguridad.")
