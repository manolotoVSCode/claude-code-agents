# Agentes para Claude Code

[English](README.en.md)

Cinco subagentes para [Claude Code](https://code.claude.com/docs/en/sub-agents) que diseñé y escribí para mis proyectos, con los hooks que restringen lo que pueden ejecutar. Este repositorio trae sus versiones genéricas.

Hecho por Manuel de la Torre ([manoloto.com](https://manoloto.com)).

> Proyecto personal, no afiliado a Anthropic.

| Agente | Qué hace | Cómo se hace cumplir |
|---|---|---|
| `guardian` | Revisa y ensaya el SQL de escritura y decide si un PR puede hacer merge a la rama principal. Escribe aprobaciones de un solo uso, ligadas al hash exacto del SQL o al número del PR. | `guardian-sql.py` y `guardian-merge.py`, registrados en `settings.json`, bloquean el SQL de escritura y los merges que no tienen aprobación. |
| `code-reviewer` | Revisa cambios en solo lectura y devuelve hallazgos con severidad y `ruta:línea`, sin corregir nada. | No tiene Bash, Write ni Edit (`disallowedTools`). |
| `test-runner` | Escribe y corre pruebas con pytest y reporta fallos con su causa probable. | Un hook en su encabezado limita Bash a pytest y a `pip list` y `pip check`. |
| `security-auditor` | Busca secretos expuestos y dependencias desactualizadas, y nunca cita un secreto completo. | Un hook en su encabezado limita Bash a comandos de lectura y a `pip list`. Sin Write ni Edit. |
| `explorer` | Responde preguntas sobre el código, la documentación y el historial de git, citando la evidencia. | Un hook en su encabezado limita Bash a comandos de lectura. Sin Write ni Edit. |

Los hooks son un freno, no un entorno aislado: clasifican comandos y SQL con expresiones regulares. Antes de usarlos, lee [docs/LIMITACIONES.md](docs/LIMITACIONES.md).

## Instalación

Requisitos:

- `jq`, para los hooks de shell. Sin jq, esos hooks bloquean todos los comandos de Bash de su agente.
- Python 3.9 o posterior, para los hooks de guardian. Sin Python, esos hooks no arrancan y **no bloquean nada**.
- `gh`, con sesión iniciada, para `guardian-merge.py`. Sin gh, el hook deniega todos los merges.

```bash
git clone https://github.com/manolotoVSCode/claude-code-agents.git
```

En un proyecto (los agentes quedan en `.claude/agents/` y los hooks en `.claude/hooks/`):

```bash
./claude-code-agents/install.sh --proyecto /ruta/a/tu/proyecto
```

Para todos tus proyectos (en `~/.claude/`):

```bash
./claude-code-agents/install.sh --usuario
```

Agrega `--idioma en` para instalar los prompts en inglés. El script no sobrescribe archivos existentes sin preguntar y no modifica ningún `settings.json`.

No lo empaqueté como plugin a propósito: Claude Code ignora el campo `hooks` en los agentes de un plugin, así que test-runner, security-auditor y explorer quedarían con Bash sin restricción y sin ningún aviso.

### Activar el guardián

Los hooks de guardian no van en el encabezado del agente, porque deben vigilar a la sesión principal, no al guardián. Se registran en `.claude/settings.json` del proyecto:

1. Copia los bloques de [examples/settings.hooks.json](examples/settings.hooks.json) dentro de la clave `hooks` de tu `settings.json`.
2. Cambia `mcp__db__execute_sql|mcp__db__apply_migration` por los nombres de las herramientas de tu servidor MCP que ejecutan SQL. El formato es `mcp__<servidor>__<herramienta>`.
3. Agrega esas mismas herramientas a la línea `tools` de `guardian.md` para que el guardián pueda ensayar el SQL.
4. Si instalaste en `~/.claude/`, cambia `${CLAUDE_PROJECT_DIR}/.claude/hooks/` por la ruta absoluta de tu carpeta `~/.claude/hooks/`.

Las aprobaciones, los respaldos y el registro quedan en `.claude/guardian/` del proyecto. Agrega esa carpeta a tu `.gitignore` si no quieres versionarla.

## Configuración

Variables de entorno que leen los hooks. Puedes definirlas en la clave `env` de `settings.json`.

| Variable | Hook | Para qué | Por defecto |
|---|---|---|---|
| `CLAUDE_AGENTS_VENV` | test-runner, security-auditor | Carpeta del entorno virtual, relativa al proyecto. | `.venv` |
| `GUARDIAN_RAMAS` | guardian-merge | Ramas protegidas, separadas por comas. | `main,master` |
| `GUARDIAN_VENTANA_BLOQUEO` | guardian-merge | Horario en que solo pasan los merges urgentes, por ejemplo `1-5 07:00-21:00` (días ISO, 1 = lunes). | sin ventana |
| `GUARDIAN_ZONA` | guardian-merge | Zona horaria de la ventana, por ejemplo `America/Mexico_City`. | la del sistema |
| `GUARDIAN_ESQUEMAS_PROTEGIDOS` | guardian-sql | Esquemas cuya mención en una escritura la vuelve ROJA. | `pg_catalog,information_schema` |

## Cómo funciona el guardián

`guardian-sql.py` clasifica cada SQL en tres niveles:

- **VERDE:** lectura, o un ensayo `BEGIN ... ROLLBACK`. Pasa.
- **ÁMBAR:** escritura recuperable. Pasa solo si existe `.claude/guardian/aprobaciones/<sha256>.json`, donde sha256 es el hash del SQL exacto.
- **ROJO:** DROP, TRUNCATE, UPDATE o DELETE sin WHERE, permisos, roles, desactivar RLS o triggers. Además del archivo, exige el campo `aprobacion_humana`.

Cuando falta la aprobación, el hook deniega y le dice al agente principal que invoque al guardián con ese SQL. El guardián lo ensaya, guarda un respaldo y el SQL de reversa, y escribe la aprobación. Al usarse, el hook la renombra a `.usada`, así que sirve una sola vez. Todo queda en `.claude/guardian/registro.log`. Ante un error interno, el hook deniega.

`guardian-merge.py` hace lo mismo con `gh pr merge <N>`: exige el número de PR, rechaza `--admin`, exige CI completo y en verde y un archivo `pr-<N>.json` con veredicto `merge`, y lo consume. También bloquea `git push` directo a las ramas protegidas.

## Pruebas

```bash
python3 -m unittest discover -s tests -v
```

Las pruebas alimentan cada hook con el JSON que recibiría de Claude Code y comprueban si deja pasar o bloquea. No usan red: `gh` se simula. En macOS, con bash 3.2.57 y Python 3.9.6, pasan las 49.

## Licencia

[MIT](LICENSE).
