#!/bin/bash
# Hook PreToolUse para el subagente `test-runner`.
# Deja pasar solo corridas de pytest e inspección de pip; cualquier otro
# comando se bloquea con exit 2.
#
# Permitido (primera etapa de un pipeline):
#   pytest [args]                      <venv>/bin/pytest [args]
#   python|python3 -m pytest [args]    <venv>/bin/python|python3 -m pytest [args]
#   pip|pip3 list|check                <venv>/bin/pip|pip3 list|check
#   python|python3 -m pip list|check
# Permitido tras un pipe (solo filtros de salida): head, tail, grep, wc, cat
#
# <venv> es la carpeta del entorno virtual, relativa a la raíz del proyecto.
# Se configura con la variable CLAUDE_AGENTS_VENV (por defecto, .venv).
#
# Bloqueado: pip install/uninstall, prefijos VAR=valor, cadenas de conexión a
# bases de datos, redirecciones (salvo el literal 2>&1), sustitución de
# comandos y cualquier otro binario.
#
# Entrada: el JSON del hook por stdin ({ tool_name, tool_input: { command } }).
# Requiere jq. Sin jq, o ante un error interno, bloquea.
set -u
set -f   # sin expansión de comodines al partir el comando en palabras

venv="${CLAUDE_AGENTS_VENV:-.venv}"
venv="${venv%/}"

if ! command -v jq >/dev/null 2>&1; then
  echo "test-runner: falta jq; comando bloqueado por precaución" >&2
  exit 2
fi
input=$(cat)
if ! printf '%s' "$input" | jq . >/dev/null 2>&1; then
  echo "test-runner: el input del hook no se pudo leer como JSON; comando bloqueado por precaución" >&2
  exit 2
fi
cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // empty') || exit 2
[ -z "$cmd" ] && exit 0   # sin tool_input.command: no es un evento Bash, no bloquear

deny() {
  echo "test-runner: comando bloqueado — $1" >&2
  echo "Comando: $cmd" >&2
  exit 2
}

# coincide <regex-extendida> <texto> [-i]: 0 si coincide, 1 si no.
# Cualquier otro resultado de grep es un error y bloquea (falla cerrada).
coincide() {
  local r
  printf '%s\n' "$2" | grep -Eq ${3:-} -- "$1"
  r=$?
  [ "$r" -le 1 ] || deny "error interno al evaluar el comando"
  return "$r"
}

# --- Bloqueos globales --------------------------------------------------------
coincide 'DATABASE_URL|DIRECT_URL|(postgres(ql)?|mysql|mariadb|mongodb(\+srv)?|redis|amqp)://' "$cmd" -i \
  && deny "referencia a una base de datos o cadena de conexión; las pruebas no tocan bases reales"

coincide '(^|[^[:alnum:]_])pip3?([[:space:]]+-[^[:space:]]+)*[[:space:]]+(install|uninstall|download|wheel)([[:space:]]|$)' "$cmd" -i \
  && deny "pip install/uninstall no está permitido; el entorno lo gestiona el agente principal"

# Redirecciones: se tolera el literal '2>&1' y se bloquea todo lo demás.
stripped="${cmd//2>&1/}"
case "$stripped" in
  *'>'*) deny "redirección de salida (>)" ;;
  *'<'*) deny "redirección de entrada (<)" ;;
esac
coincide '\$\(|`' "$cmd" && deny "sustitución de comandos"
coincide '(^|[^[:alnum:]_./-])(tee|xargs|rm|mv|cp|ln|chmod|chown|touch|mkdir|rmdir|truncate|dd|curl|wget|kill|killall|sudo|eval|exec|source|env|export|nohup|open|osascript|psql|mysql|createdb|dropdb|git|node|npm|npx|bun|pnpm|yarn|perl|ruby|sh|bash|zsh)([[:space:]]|$)' "$cmd" \
  && deny "binario no permitido"

# --- Revisión por comando -----------------------------------------------------
# Los comandos unidos con && ; || se validan uno por uno. Dentro de un comando,
# las etapas después de un | solo pueden ser filtros de salida. Los separadores
# entre comillas (por ejemplo, pytest -k "a or b") no cuentan como separadores.
split_unquoted() {
  # $1: texto de entrada; $2: caracteres que actúan como separadores (| ; &)
  local in="$1" seps="$2" out="" q="" c i n=${#1}
  for ((i = 0; i < n; i++)); do
    c="${in:i:1}"
    if [ -n "$q" ]; then
      [ "$c" = "$q" ] && q=""
      out+="$c"
    elif [ "$c" = '"' ] || [ "$c" = "'" ]; then
      q="$c"; out+="$c"
    elif [ "$c" = "\\" ]; then
      out+="$c${in:i+1:1}"; ((i++))
    elif [[ "$seps" == *"$c"* ]]; then
      out+=$'\n'
    else
      out+="$c"
    fi
  done
  printf '%s' "$out"
}

# Argumentos de pytest: nada que salga del proyecto.
check_pytest_args() {
  local arg
  for arg in "$@"; do
    case "$arg" in
      ../*|*/../*|..) deny "pytest no admite rutas fuera del proyecto ('$arg')" ;;
    esac
  done
}

# Subcomando de pip: solo list y check.
check_pip_sub() {
  case "${1:-}" in
    list|check) ;;
    *) deny "pip ${1:-<vacío>} no está en la lista (solo list, check)" ;;
  esac
}

es_de() {
  # es_de <primera-palabra> <nombre>: verdadero si es el binario a secas o el del venv.
  [ "$1" = "$2" ] || [ "$1" = "$venv/bin/$2" ] || [ "$1" = "./$venv/bin/$2" ]
}

IFS=$'\n'
lineas=( $(split_unquoted "$stripped" ';&') )
unset IFS
evaluados=0

for line in ${lineas[@]+"${lineas[@]}"}; do
  IFS=$'\n'
  etapas=( $(split_unquoted "$line" '|') )
  unset IFS
  stage_no=0
  for seg in ${etapas[@]+"${etapas[@]}"}; do
    seg="${seg#"${seg%%[![:space:](]*}"}"   # quita espacios y '(' iniciales
    [ -z "${seg// /}" ] && continue
    stage_no=$((stage_no + 1))
    evaluados=$((evaluados + 1))

    coincide '^[A-Za-z_][A-Za-z0-9_]*=' "$seg" && deny "asignación de variable de entorno en línea"

    set -- $seg
    first="$1"; shift

    if [ "$stage_no" -gt 1 ]; then
      case "$first" in
        head|tail|grep|wc|cat) ;;
        *) deny "'$first' no es un filtro de salida permitido tras un pipe" ;;
      esac
      continue
    fi

    if es_de "$first" pytest; then
      check_pytest_args "$@"
    elif es_de "$first" python || es_de "$first" python3; then
      [ "${1:-}" = "-m" ] || deny "python solo se permite como 'python -m pytest' o 'python -m pip list|check'"
      case "${2:-}" in
        pytest) shift 2; check_pytest_args "$@" ;;
        pip)    check_pip_sub "${3:-}" ;;
        *) deny "python -m ${2:-<vacío>} no está en la lista (solo pytest, pip list|check)" ;;
      esac
    elif es_de "$first" pip || es_de "$first" pip3; then
      check_pip_sub "${1:-}"
    else
      deny "'$first' no está en la lista de comandos del test-runner"
    fi
  done
done

# Si no se revisó ningún comando, algo falló al partir la entrada: se bloquea.
[ "$evaluados" -gt 0 ] || deny "no se pudo analizar el comando"
exit 0
