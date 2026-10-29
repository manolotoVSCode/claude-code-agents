#!/usr/bin/env python3
"""Guardián de merges a la rama principal.

Hook PreToolUse sobre Bash. Actúa si el comando invoca `gh pr merge` o un
`git push` a una rama protegida. Para un merge exige:

  1. número de PR explícito en el comando y sin --admin;
  2. PR abierto y CI completo y en verde (al menos un check, ninguno
     fallido ni pendiente), consultado con `gh pr view`;
  3. .claude/guardian/aprobaciones/pr-<N>.json escrito por el agente guardian
     con "veredicto": "merge" o "merge_urgente";
  4. si hay ventana de bloqueo configurada y es hora de bloqueo, solo pasa
     "merge_urgente".

La aprobación se consume al usarse. Bloquea también `git push` directo a una
rama protegida y la API de merge de gh. Ante cualquier error, deniega.

Configuración por variables de entorno:
  GUARDIAN_RAMAS            ramas protegidas, separadas por comas
                            (por defecto, main,master).
  GUARDIAN_VENTANA_BLOQUEO  horario en que solo pasan los merges urgentes, con
                            la forma "1-5 07:00-21:00" (días ISO, 1 = lunes;
                            la hora final no se incluye). Vacío o sin definir:
                            sin ventana.
  GUARDIAN_ZONA             zona horaria IANA de la ventana, como
                            "America/Mexico_City". Por defecto, la del sistema.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime

RAIZ = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
DIR_G = os.path.join(RAIZ, ".claude", "guardian")
OK = {"SUCCESS", "SKIPPED", "NEUTRAL"}
RAMAS = [r.strip() for r in os.environ.get("GUARDIAN_RAMAS", "main,master").split(",") if r.strip()]

# Solo cuenta como invocación lo que empieza un comando: inicio de línea o tras
# ; & | ( o un prefijo como sudo/env/command.
INICIO = r"(?:^|[;&|(]|\$\()\s*(?:(?:sudo|env|command|exec|time|xargs|nohup)\s+(?:-\S+\s+)*)*"
# Si el comando pasa texto a un intérprete, lo que va entre comillas también se
# ejecuta, así que no se descarta.
INTERPRETE = re.compile(r"(?:^|[\s;&|(])(?:bash|sh|zsh|dash|ksh|eval|source|xargs)(?:\s|$)")
# Con un intérprete, cualquier palabra puede empezar un comando.
INICIO_LAXO = r"(?:^|[\s;&|('\"]|\$\()\s*"


def registrar(linea: str) -> None:
    try:  # el registro es auditoría; si falla, la decisión se entrega igual
        os.makedirs(DIR_G, exist_ok=True)
        with open(os.path.join(DIR_G, "registro.log"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {linea}\n")
    except OSError:
        pass


def denegar(motivo: str) -> None:
    registrar(f"MERGE BLOQUEADO {motivo}")
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"Guardián de merge: {motivo}",
        }
    }, ensure_ascii=False))
    sys.exit(0)


def preparar(cmd: str) -> tuple[str, str]:
    """Devuelve (texto a revisar, patrón de inicio de comando).

    Sin intérprete, vacía el contenido entre comillas: así no se bloquea un
    texto que solo menciona el comando (un mensaje de commit, un echo). Con
    intérprete, lo que va entre comillas también se ejecuta, así que se revisa
    todo y cualquier palabra puede empezar un comando (`bash -c 'gh pr merge 7'`).
    """
    if INTERPRETE.search(cmd):
        return cmd, INICIO_LAXO
    return re.sub(r"'[^']*'|\"(?:\\.|[^\"\\])*\"", "''", cmd), INICIO


def revisar_push(cmd: str, inicio: str) -> None:
    """Deniega un git push que pueda llegar a una rama protegida."""
    for m in re.finditer(inicio + r"git(?:\s+-[cC]\s+\S+)*\s+push\b([^;&|\n]*)", cmd, re.M):
        try:
            args = shlex.split(m.group(1))
        except ValueError:
            denegar("no pude leer los argumentos de git push.")
        if any(a in ("--all", "--mirror") for a in args):
            denegar("git push --all o --mirror puede llegar a una rama protegida.")
        posicionales = [a for a in args if not a.startswith("-")]
        refspecs = posicionales[1:] or ["HEAD"]  # sin refspec, git empuja la rama actual
        for ref in refspecs:
            destino = ref.lstrip("+").split(":")[-1]
            destino = destino[len("refs/heads/"):] if destino.startswith("refs/heads/") else destino
            if destino == "HEAD":
                destino = rama_actual()
            if destino in RAMAS:
                denegar(f"push directo a {destino} prohibido; usa un PR.")


def rama_actual() -> str:
    out = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                         capture_output=True, text=True, timeout=10, cwd=RAIZ)
    actual = out.stdout.strip()
    if out.returncode != 0 or not actual:
        denegar("no pude saber la rama actual para revisar git push.")
    return actual


def en_ventana_de_bloqueo() -> bool:
    ventana = os.environ.get("GUARDIAN_VENTANA_BLOQUEO", "").strip()
    if not ventana:
        return False
    m = re.fullmatch(r"([1-7])-([1-7])\s+(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})", ventana)
    if not m:
        raise ValueError(f"GUARDIAN_VENTANA_BLOQUEO no tiene la forma '1-5 07:00-21:00': {ventana!r}")
    d1, d2, h1, m1, h2, m2 = (int(x) for x in m.groups())
    zona = os.environ.get("GUARDIAN_ZONA", "").strip()
    if zona:
        from zoneinfo import ZoneInfo
        ahora = datetime.now(ZoneInfo(zona))
    else:
        ahora = datetime.now().astimezone()
    dia = ahora.isoweekday()
    minuto = ahora.hour * 60 + ahora.minute
    return d1 <= dia <= d2 and h1 * 60 + m1 <= minuto < h2 * 60 + m2


def main() -> None:
    evento = json.load(sys.stdin)
    cmd = str((evento.get("tool_input") or {}).get("command", "") or "")
    visible, inicio = preparar(cmd)

    revisar_push(visible, inicio)

    if re.search(r"\bgh\s+api\b.*/pulls/\d+/merge\b", visible):
        denegar("el merge por la API no pasa por el guardián; usa gh pr merge <N>.")

    if not re.search(inicio + r"gh\s+pr\s+merge\b", visible, re.M):
        sys.exit(0)

    m = re.search(inicio + r"gh\s+pr\s+merge\s+(?:#)?(\d+)\b", visible, re.M)
    if not m:
        denegar("indica el número de PR explícito (gh pr merge <N> ...).")
    n = m.group(1)

    if "--admin" in cmd:
        denegar("--admin salta las protecciones; no permitido.")

    try:
        out = subprocess.run(
            ["gh", "pr", "view", n, "--json", "statusCheckRollup,baseRefName,state"],
            capture_output=True, text=True, timeout=30, cwd=RAIZ,
        )
        datos = json.loads(out.stdout or "{}")
    except Exception as exc:  # sin red o sin gh: no se arriesga
        denegar(f"no pude consultar el PR #{n} ({exc}).")

    if datos.get("state") != "OPEN":
        denegar(f"PR #{n} no está abierto.")
    checks = datos.get("statusCheckRollup") or []
    if not checks:
        denegar(f"PR #{n} sin checks de CI; espera a que CI corra.")
    malos = [
        c.get("name") or c.get("context") or "?"
        for c in checks
        if (c.get("conclusion") or c.get("state") or "").upper() not in OK
    ]
    if malos:
        denegar(f"PR #{n} con CI no verde o pendiente: {', '.join(malos)}.")

    ruta = os.path.join(DIR_G, "aprobaciones", f"pr-{n}.json")
    if not os.path.exists(ruta):
        denegar(f"falta la aprobación del agente guardian para el PR #{n}.")
    with open(ruta, encoding="utf-8") as f:
        aprob = json.load(f)
    if not isinstance(aprob, dict):
        raise ValueError("la aprobación no es un objeto JSON")
    veredicto = aprob.get("veredicto")
    if veredicto not in ("merge", "merge_urgente"):
        denegar(f"el guardian no dio veredicto 'merge' al PR #{n}: {aprob.get('motivo', '')}")

    if veredicto != "merge_urgente" and en_ventana_de_bloqueo():
        denegar(f"dentro de la ventana de bloqueo ({os.environ.get('GUARDIAN_VENTANA_BLOQUEO')}). "
                f"El PR #{n} queda aprobado para después; no reescribas la aprobación.")

    os.replace(ruta, ruta + ".usada")
    registrar(f"MERGE PERMITIDO PR #{n} ({aprob.get('resumen', '')})")
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": f"Guardián: PR #{n} con CI verde y revisión aprobada.",
        }
    }, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        denegar(f"error interno ({exc!r}); se deniega por seguridad.")
