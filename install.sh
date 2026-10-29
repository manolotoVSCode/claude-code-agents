#!/bin/bash
# Instala los agentes y sus hooks en un proyecto o en tu carpeta de usuario.
#
#   ./install.sh --proyecto RUTA [--idioma es|en] [--forzar]
#   ./install.sh --usuario       [--idioma es|en] [--forzar]
#
# --proyecto RUTA  copia a RUTA/.claude/agents y RUTA/.claude/hooks.
# --usuario        copia a ~/.claude/agents y ~/.claude/hooks, y ajusta la
#                  ruta de los hooks en el encabezado de cada agente.
# --idioma         idioma de los prompts de los agentes (por defecto, es).
# --forzar         sobrescribe archivos existentes sin preguntar.
#
# No modifica ningún settings.json. Los hooks de guardian se registran a mano;
# ver examples/settings.hooks.json y el README.
set -eu

origen="$(cd "$(dirname "$0")" && pwd)"
destino=""
alcance=""
idioma="es"
forzar=""

uso() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --proyecto) [ "$#" -ge 2 ] || uso 1; alcance="proyecto"; destino="$2"; shift 2 ;;
    --usuario)  alcance="usuario"; destino="$HOME"; shift ;;
    --idioma)   [ "$#" -ge 2 ] || uso 1; idioma="$2"; shift 2 ;;
    --forzar)   forzar=1; shift ;;
    -h|--help)  uso 0 ;;
    *) echo "Opción desconocida: $1" >&2; uso 1 ;;
  esac
done

[ -n "$alcance" ] || { echo "Indica --proyecto RUTA o --usuario." >&2; uso 1; }
[ -d "$origen/agents/$idioma" ] || { echo "Idioma no disponible: $idioma (usa es o en)." >&2; exit 1; }
[ -d "$destino" ] || { echo "No existe la carpeta de destino: $destino" >&2; exit 1; }
destino="$(cd "$destino" && pwd)"

dir_agentes="$destino/.claude/agents"
dir_hooks="$destino/.claude/hooks"
mkdir -p "$dir_agentes" "$dir_hooks"

# copiar <origen> <destino>: no sobrescribe sin confirmación.
copiar() {
  local de="$1" a="$2" resp
  if [ -e "$a" ] && [ -z "$forzar" ]; then
    if cmp -s "$de" "$a"; then
      echo "  igual, sin cambios: $a"; return 1
    fi
    if [ -t 0 ]; then
      printf '  ya existe %s. ¿Sobrescribir? [s/N] ' "$a"
      read -r resp
      case "$resp" in s|S|si|sí|y|Y) ;; *) echo "  se conserva: $a"; return 1 ;; esac
    else
      echo "  ya existe, se conserva (usa --forzar para sobrescribir): $a"; return 1
    fi
  fi
  cp "$de" "$a"
  echo "  copiado: $a"
}

echo "Agentes ($idioma) en $dir_agentes"
for f in "$origen/agents/$idioma"/*.md; do
  temporal="$dir_agentes/.$(basename "$f").instalando"
  if [ "$alcance" = "usuario" ]; then
    # En el alcance de usuario no hay proyecto fijo: el hook va por ruta absoluta.
    ruta_hooks="$dir_hooks"
    sed "s|\${CLAUDE_PROJECT_DIR}/.claude/hooks/|${ruta_hooks//|/\\|}/|g" "$f" >"$temporal"
  else
    cp "$f" "$temporal"
  fi
  copiar "$temporal" "$dir_agentes/$(basename "$f")" || true
  rm -f "$temporal"
done

echo "Hooks en $dir_hooks"
for f in "$origen/hooks"/*; do
  if copiar "$f" "$dir_hooks/$(basename "$f")"; then
    chmod +x "$dir_hooks/$(basename "$f")"
  fi
done

echo
faltan=""
command -v jq >/dev/null 2>&1      || faltan="$faltan\n  - jq: sin jq, los hooks de shell bloquean todos los comandos de Bash de su agente."
command -v python3 >/dev/null 2>&1 || faltan="$faltan\n  - python3: los hooks de guardian no pueden correr. Un hook que no arranca no bloquea nada."
command -v gh >/dev/null 2>&1      || faltan="$faltan\n  - gh: guardian-merge.py deniega todos los merges porque no puede consultar el PR."
if [ -n "$faltan" ]; then
  printf 'Atención, faltan dependencias:%b\n\n' "$faltan"
fi

cat <<EOF
Listo. Pasos que faltan:
  1. Los hooks de guardian no se activan solos. Copia los bloques de
     examples/settings.hooks.json a .claude/settings.json del proyecto y cambia
     "mcp__db__" por el nombre de tu servidor MCP de base de datos.
  2. Agrega a la línea "tools" de guardian.md las herramientas MCP con las que
     ensaya el SQL.
  3. Si es la primera vez que existe la carpeta de agentes, reinicia Claude Code.
  4. Los hooks del encabezado de un agente de proyecto solo corren si confías
     en la carpeta del proyecto.
EOF
