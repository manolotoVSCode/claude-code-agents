"""Pruebas de los hooks PreToolUse.

Cada caso alimenta un hook con el JSON que Claude Code le pasaría por stdin y
comprueba su decisión: en los hooks de shell, exit 0 deja pasar y exit 2
bloquea; en los de guardian, la decisión va en hookSpecificOutput.

Uso:
    python3 -m unittest discover -s tests -v
    HOOKS_DIR=/otra/carpeta python3 -m unittest discover -s tests -v

Requiere bash, jq y Python 3.9 o posterior. No usa red: gh se simula.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest

AQUI = os.path.dirname(os.path.abspath(__file__))
HOOKS = os.environ.get("HOOKS_DIR") or os.path.join(AQUI, "..", "hooks")
# Rutas absolutas: algunas pruebas cambian PATH para simular un entorno roto.
BASH = shutil.which("bash") or "/bin/bash"
PYTHON = shutil.which("python3") or "python3"


def ejecutar(hook: str, entrada, env: dict | None = None, cwd: str | None = None):
    """Corre el hook con `entrada` (dict o texto crudo) y devuelve (código, decisión, stderr)."""
    ruta = os.path.join(HOOKS, hook)
    datos = entrada if isinstance(entrada, str) else json.dumps(entrada)
    interprete = [PYTHON] if hook.endswith(".py") else [BASH]
    entorno = dict(os.environ, **(env or {}))
    r = subprocess.run(interprete + [ruta], input=datos, capture_output=True, text=True,
                       env=entorno, cwd=cwd, timeout=60)
    decision = None
    if r.stdout.strip():
        try:
            decision = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"]
        except (ValueError, KeyError, TypeError):
            decision = "salida-ilegible"
    return r.returncode, decision, r.stderr + r.stdout


def bash(cmd: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


class HookDeShell(unittest.TestCase):
    hook = ""
    pasan: list = []
    bloquea: list = []

    def comprobar(self, casos, esperado):
        for cmd in casos:
            entrada = cmd if cmd.startswith("{malformado") else bash(cmd)
            with self.subTest(comando=cmd):
                codigo, _, err = ejecutar(self.hook, entrada)
                self.assertEqual(codigo, esperado, f"{self.hook} con {cmd!r}: {err.strip()[:200]}")

    def test_deja_pasar(self):
        if self.hook:
            self.comprobar(self.pasan, 0)

    def test_bloquea(self):
        if self.hook:
            self.comprobar(self.bloquea, 2)


class TestRunner(HookDeShell):
    hook = "test-runner-bash.sh"
    pasan = [
        ".venv/bin/python -m pytest -q",
        "pytest tests/ | head",
        "pytest -q 2>&1 | tail -20",
        "python3 -m pytest -k 'a or b' tests/",
        ".venv/bin/pip list",
        "pip check && pytest -q",
    ]
    bloquea = [
        "pip install x",
        "FOO=1 pytest",
        "pytest > salida.txt",
        "pytest | rm x",
        "git status",
        "python -c 'print(1)'",
        "pytest ../otro-proyecto",
        "pytest $(echo tests)",
        "DATABASE_URL=postgres://u:p@h/db pytest",
        "{malformado",
    ]


class SecurityAuditor(HookDeShell):
    hook = "security-auditor-bash.sh"
    pasan = [
        "git log --oneline -5",
        "grep -rn password .",
        "ls",
        "pip list --outdated 2>&1 | head",
        "grep -rnE 'postgres(ql)?://|api_key' . | wc -l",
        "git log -p --all -S 'secreto' | head -50",
    ]
    bloquea = [
        "curl https://x",
        "pip install x",
        "sed -i s/a/b/ f",
        "find . -delete",
        "git push",
        "python -m pip list --index-url http://x",
        "grep -rn password ~/",
        "ls > lista.txt",
        "{malformado",
    ]


class Explorer(HookDeShell):
    hook = "explorer-readonly-bash.sh"
    pasan = [
        "ls -la",
        "git log --oneline -5",
        "git --no-pager show HEAD --stat",
        "grep -rn 'a|b' src | head",
        "find . -maxdepth 2 -type d -not -path '*/.git/*'",
        "sed -n 1,20p README.md",
        "jq .name package.json",
    ]
    bloquea = [
        "rm -rf x",
        "python3 -c 'print(1)'",
        "curl https://x",
        "find . -delete",
        "find . -exec cat {} ;",
        "sed -i s/a/b/ f",
        "ls > x",
        "echo $(id)",
        "git checkout main",
        "git push",
        "npm install",
        "{malformado",
    ]


class Sospechas(unittest.TestCase):
    """Defectos que sospeché al leer el código. Cada caso dice qué debería pasar."""

    def caso(self, hook, cmd, esperado):
        with self.subTest(hook=hook, comando=cmd):
            codigo, _, err = ejecutar(hook, bash(cmd))
            self.assertEqual(codigo, esperado, f"{hook} con {cmd!r}: {err.strip()[:200]}")

    def test_explorer_git_que_escribe(self):
        for cmd in ("git tag v9", "git remote add x https://x", "git branch nueva"):
            self.caso("explorer-readonly-bash.sh", cmd, 2)

    def test_explorer_tree_y_rg_pre(self):
        for cmd in ("tree -o salida.txt", "rg --pre cat patron ."):
            self.caso("explorer-readonly-bash.sh", cmd, 2)

    def test_sed_n_con_w_escribe(self):
        for hook in ("explorer-readonly-bash.sh", "security-auditor-bash.sh"):
            self.caso(hook, "sed -n 'w copia.txt' archivo", 2)

    def test_auditor_escrituras_y_ejecucion(self):
        for cmd in ("git diff --output=parche.txt",
                    "awk 'BEGIN{system(\"id\")}'",
                    "rg --pre cat patron ."):
            self.caso("security-auditor-bash.sh", cmd, 2)

    def test_explorer_awk_que_ejecuta(self):
        for cmd in ("awk 'BEGIN{system(\"id\")}'", "awk '{print | \"id\"}' f"):
            self.caso("explorer-readonly-bash.sh", cmd, 2)


SHELL = ("test-runner-bash.sh", "security-auditor-bash.sh", "explorer-readonly-bash.sh")


class FallaCerrada(unittest.TestCase):
    """Si el entorno falla, los hooks de shell deben bloquear, no dejar pasar."""

    def test_sin_carpeta_temporal_escribible(self):
        for hook in SHELL:
            with self.subTest(hook=hook):
                codigo, _, err = ejecutar(hook, bash("rm -rf x"), {"TMPDIR": "/no/existe"})
                self.assertEqual(codigo, 2, err.strip()[:200])

    def test_grep_que_falla(self):
        bin_ = tempfile.mkdtemp()
        try:
            with open(os.path.join(bin_, "grep"), "w") as f:
                f.write("#!/bin/sh\nexit 3\n")
            os.chmod(os.path.join(bin_, "grep"), 0o755)
            for hook in SHELL:
                with self.subTest(hook=hook):
                    env = {"PATH": bin_ + os.pathsep + os.environ["PATH"]}
                    codigo, _, err = ejecutar(hook, bash("ls"), env)
                    self.assertEqual(codigo, 2, err.strip()[:200])
        finally:
            shutil.rmtree(bin_)

    def test_sin_jq(self):
        bin_ = tempfile.mkdtemp()
        try:
            for b in ("cat", "grep", "printf"):
                ruta = shutil.which(b)
                if ruta:
                    os.symlink(ruta, os.path.join(bin_, b))
            for hook in SHELL:
                with self.subTest(hook=hook):
                    codigo, _, err = ejecutar(hook, bash("ls"), {"PATH": bin_})
                    self.assertEqual(codigo, 2, err.strip()[:200])
        finally:
            shutil.rmtree(bin_)

    def test_venv_configurable(self):
        env = {"CLAUDE_AGENTS_VENV": "entorno"}
        self.assertEqual(ejecutar("test-runner-bash.sh", bash("entorno/bin/python -m pytest -q"), env)[0], 0)
        self.assertEqual(ejecutar("test-runner-bash.sh", bash("otro/bin/python -m pytest -q"), env)[0], 2)


# --- guardian-merge -----------------------------------------------------------

GH_FALSO = """#!/bin/bash
# gh simulado: responde con el JSON guardado en $GH_RESPUESTA
if [ "$1 $2" = "pr view" ]; then cat "$GH_RESPUESTA"; exit 0; fi
exit 1
"""


class GuardianMerge(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        gh = os.path.join(self.bin, "gh")
        with open(gh, "w") as f:
            f.write(GH_FALSO)
        os.chmod(gh, os.stat(gh).st_mode | stat.S_IEXEC)
        self.respuesta = os.path.join(self.dir, "respuesta.json")
        self.pr(state="OPEN", checks=[{"name": "ci", "conclusion": "SUCCESS"}])
        self.aprob = os.path.join(self.dir, ".claude", "guardian", "aprobaciones")
        os.makedirs(self.aprob)
        self.env = {"CLAUDE_PROJECT_DIR": self.dir, "GH_RESPUESTA": self.respuesta,
                    "PATH": self.bin + os.pathsep + os.environ["PATH"]}

    def tearDown(self):
        shutil.rmtree(self.dir)

    def pr(self, state, checks):
        with open(self.respuesta, "w") as f:
            json.dump({"state": state, "baseRefName": "main", "statusCheckRollup": checks}, f)

    def aprobar(self, n, veredicto="merge"):
        with open(os.path.join(self.aprob, f"pr-{n}.json"), "w") as f:
            json.dump({"veredicto": veredicto, "resumen": "prueba"}, f)

    def decision(self, cmd):
        codigo, decision, err = ejecutar("guardian-merge.py", bash(cmd), self.env)
        self.assertEqual(codigo, 0, err)
        return decision

    def test_deniega_sin_numero(self):
        self.assertEqual(self.decision("gh pr merge --squash"), "deny")

    def test_deniega_admin(self):
        self.aprobar(7)
        self.assertEqual(self.decision("gh pr merge 7 --admin"), "deny")

    def test_deniega_push_a_main(self):
        self.assertEqual(self.decision("git push origin main"), "deny")

    def test_deniega_ci_pendiente(self):
        self.pr("OPEN", [{"name": "ci", "conclusion": None, "status": "IN_PROGRESS"}])
        self.aprobar(7)
        self.assertEqual(self.decision("gh pr merge 7 --squash"), "deny")

    def test_deniega_sin_aprobacion(self):
        self.assertEqual(self.decision("gh pr merge 7 --squash"), "deny")

    def test_deniega_aprobacion_ilegible(self):
        with open(os.path.join(self.aprob, "pr-7.json"), "w") as f:
            f.write("{no es json")
        self.assertEqual(self.decision("gh pr merge 7 --squash"), "deny")

    def test_permite_una_vez(self):
        self.aprobar(7)
        self.assertEqual(self.decision("gh pr merge 7 --squash"), "allow")
        self.assertEqual(self.decision("gh pr merge 7 --squash"), "deny")

    def test_ignora_mencion_en_texto(self):
        self.assertIsNone(self.decision('echo "luego corre gh pr merge 7"'))

    def test_sospecha_push_a_rama_que_contiene_main(self):
        self.assertIsNone(self.decision("git push origin feature/main-fix"))

    def test_sospecha_mencion_tras_punto_y_coma_en_comillas(self):
        self.assertIsNone(self.decision('git commit -m "listo; gh pr merge 7 despues"'))

    def test_deniega_merge_dentro_de_bash_c(self):
        self.assertEqual(self.decision("bash -c 'gh pr merge 7 --squash'"), "deny")

    def test_deniega_merge_por_la_api(self):
        self.assertEqual(self.decision("gh api -X PUT repos/o/r/pulls/7/merge"), "deny")

    def test_deniega_push_con_refspec_a_main(self):
        for cmd in ("git push origin HEAD:main", "git push origin +main",
                    "git push origin refs/heads/main", "git push --all"):
            with self.subTest(comando=cmd):
                self.assertEqual(self.decision(cmd), "deny")

    def test_ventana_de_bloqueo(self):
        self.aprobar(7)
        self.env["GUARDIAN_VENTANA_BLOQUEO"] = "1-7 00:00-24:00"  # siempre
        self.assertEqual(self.decision("gh pr merge 7 --squash"), "deny")
        self.aprobar(7, "merge_urgente")
        self.assertEqual(self.decision("gh pr merge 7 --squash"), "allow")

    def test_ventana_mal_escrita_deniega(self):
        self.aprobar(7)
        self.env["GUARDIAN_VENTANA_BLOQUEO"] = "de lunes a viernes"
        self.assertEqual(self.decision("gh pr merge 7 --squash"), "deny")


# --- guardian-sql -------------------------------------------------------------

class GuardianSQL(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.aprob = os.path.join(self.dir, ".claude", "guardian", "aprobaciones")
        os.makedirs(self.aprob)
        self.env = {"CLAUDE_PROJECT_DIR": self.dir}

    def tearDown(self):
        shutil.rmtree(self.dir)

    def correr(self, sql, herramienta="mcp__db__execute_sql"):
        entrada = {"tool_name": herramienta, "tool_input": {"query": sql}}
        codigo, decision, err = ejecutar("guardian-sql.py", entrada, self.env)
        self.assertEqual(codigo, 0, err)
        return decision, err

    def aprobar(self, sql, **extra):
        sha = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        with open(os.path.join(self.aprob, f"{sha}.json"), "w") as f:
            json.dump(dict({"revisado_por": "guardian", "resumen": "prueba"}, **extra), f)
        return sha

    def test_select_es_verde(self):
        self.assertEqual(self.correr("select 1")[0], None)

    def test_ensayo_con_rollback_es_verde(self):
        self.assertEqual(self.correr("begin; update t set a=1 where id=2; rollback")[0], None)

    def test_insert_sin_aprobacion_incluye_sha(self):
        sql = "insert into t values (1)"
        decision, salida = self.correr(sql)
        self.assertEqual(decision, "deny")
        self.assertIn(hashlib.sha256(sql.encode()).hexdigest(), salida)

    def test_update_sin_where_es_rojo(self):
        decision, salida = self.correr("update t set a=1")
        self.assertEqual(decision, "deny")
        self.assertIn("ROJO", salida)

    def test_drop_exige_aprobacion_humana(self):
        sql = "drop table t"
        self.aprobar(sql)
        decision, salida = self.correr(sql)
        self.assertEqual(decision, "deny")
        self.assertIn("aprobacion_humana", salida)
        self.aprobar(sql, aprobacion_humana="2026-10-28: si, borrar t")
        self.assertEqual(self.correr(sql)[0], "allow")

    def test_aprobacion_de_un_solo_uso(self):
        sql = "insert into t values (1)"
        self.aprobar(sql)
        self.assertEqual(self.correr(sql)[0], "allow")
        self.assertEqual(self.correr(sql)[0], "deny")

    def test_comentario_de_bloque_no_es_verde(self):
        self.assertEqual(self.correr("select 1 /* hola */")[0], "deny")

    def test_aprobacion_ilegible_deniega(self):
        sql = "insert into t values (1)"
        sha = hashlib.sha256(sql.encode()).hexdigest()
        with open(os.path.join(self.aprob, f"{sha}.json"), "w") as f:
            f.write("{no es json")
        self.assertEqual(self.correr(sql)[0], "deny")

    def test_entrada_ilegible_deniega(self):
        codigo, decision, _ = ejecutar("guardian-sql.py", "{no es json", self.env)
        self.assertEqual((codigo, decision), (0, "deny"))

    def test_migracion_nunca_es_verde(self):
        self.assertEqual(self.correr("select 1", "mcp__db__apply_migration")[0], "deny")

    def test_registro_de_auditoria(self):
        self.correr("select 1")
        self.correr("insert into t values (1)")
        with open(os.path.join(self.dir, ".claude", "guardian", "registro.log")) as f:
            registro = f.read()
        self.assertIn("VERDE", registro)
        self.assertIn("BLOQUEADO", registro)

    def test_sin_sql_en_herramienta_que_ejecuta_deniega(self):
        codigo, decision, _ = ejecutar("guardian-sql.py",
                                       {"tool_name": "mcp__db__execute_sql", "tool_input": {"statement": "drop table t"}},
                                       self.env)
        self.assertEqual((codigo, decision), (0, "deny"))

    def test_herramienta_de_listar_sin_sql_pasa(self):
        for herramienta in ("mcp__db__list_tables", "mcp__db__list_migrations"):
            with self.subTest(herramienta=herramienta):
                codigo, decision, _ = ejecutar("guardian-sql.py",
                                               {"tool_name": herramienta, "tool_input": {}}, self.env)
                self.assertEqual((codigo, decision), (0, None))

    def test_update_con_where_y_subconsulta_es_ambar(self):
        decision, salida = self.correr("update t set a = (select b from u where c = 1) where id = 2")
        self.assertEqual(decision, "deny")
        self.assertIn("AMBAR", salida)

    def test_upsert_no_es_rojo(self):
        decision, salida = self.correr("insert into t (id, a) values (1, 2) on conflict (id) do update set a = 2")
        self.assertIn("AMBAR", salida)

    def test_sospecha_delete_sin_where_dentro_de_with(self):
        decision, salida = self.correr("with x as (delete from t returning *) select * from x")
        self.assertIn("ROJO", salida)

    def test_sospecha_update_con_where_solo_en_subconsulta(self):
        decision, salida = self.correr("update t set a = (select b from u where c = 1)")
        self.assertIn("ROJO", salida)


if __name__ == "__main__":
    unittest.main()
