#!/bin/bash
# Hook PreToolUse para el subagente `security-auditor`.
# Deja pasar solo `pip list [--outdated]` y comandos de inspección de solo
# lectura; cualquier otro comando se bloquea con exit 2.
#
# Permitido (primera etapa de un pipeline):
#   pip|pip3 list [flags]               <venv>/bin/pip|pip3 list [flags]
#   python|python3 -m pip list [flags]  (también <venv>/bin/python*)
#   ls find grep rg cat head tail wc sed -n sort uniq cut tr file stat tree
#   diff cmp nl strings column basename dirname realpath pwd date awk
#   git ls-files|log|show|grep|status|diff|blame|rev-parse|cat-file|ls-tree
# Permitido tras un pipe (solo filtros): head tail grep rg wc cat sort uniq cut
#   tr sed -n awk nl column
#
# <venv> se configura con CLAUDE_AGENTS_VENV (por defecto, .venv).
#
# Bloqueado: cualquier subcomando de pip distinto de list, binarios con acceso
# a red, prefijos VAR=valor, redirecciones (salvo el literal 2>&1), sustitución
# de comandos, opciones que escriben o ejecutan (find -exec, sed con algo
# distinto de imprimir, awk con system o getline, rg --pre, tree -o,
# --output, git --ext-diff, git grep -O) y cualquier otro binario.
#
# Las cadenas de conexión no se bloquean: buscarlas es el trabajo de este
# agente, y la lista blanca no deja ejecutar nada que pueda usarlas.
#
# Entrada: el JSON del hook por stdin ({ tool_name, tool_input: { command } }).
# Requiere jq. Sin jq, o ante un error interno, bloquea.
set -u
set -f   # sin expansión de comodines al partir el comando en palabras

venv="${CLAUDE_AGENTS_VENV:-.venv}"
venv="${venv%/}"

if ! command -v jq >/dev/null 2>&1; then
  echo "security-auditor: falta jq; comando bloqueado por precaución" >&2
  exit 2
fi
input=$(cat)
if ! printf '%s' "$input" | jq . >/dev/null 2>&1; then
  echo "security-auditor: el input del hook no se pudo leer como JSON; comando bloqueado por precaución" >&2
  exit 2
fi
cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // empty') || exit 2
[ -z "$cmd" ] && exit 0   # sin tool_input.command: no es un evento Bash, no bloquear

deny() {
  echo "security-auditor: comando bloqueado — $1" >&2
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
coincide '(^|[^[:alnum:]_])pip3?([[:space:]]+-[^[:space:]]+)*[[:space:]]+(install|uninstall|download|wheel|audit|cache|config|freeze|index|inspect|debug|hash|search|show|check)([[:space:]]|$)' "$cmd" -i \
  && deny "pip solo se permite como 'pip list [--outdated]'"
coincide '(^|[^[:alnum:]_.-])pip-audit([[:space:]]|$)' "$cmd" -i \
  && deny "pip-audit requiere red; el usuario debe correrlo fuera de esta sesión"

coincide '(^|[^[:alnum:]_./-])(curl|wget|nc|ncat|netcat|ssh|scp|sftp|rsync|telnet|ping|dig|nslookup|host|traceroute|openssl|gh|brew|npm|npx|pnpm|yarn|node|bun)([[:space:]]|$)' "$cmd" \
  && deny "binario con acceso a red no permitido"

# Redirecciones: se tolera el literal '2>&1' y se bloquea todo lo demás.
stripped="${cmd//2>&1/}"
case "$stripped" in
  *'>'*) deny "redirección de salida (>)" ;;
  *'<'*) deny "redirección de entrada (<)" ;;
esac
coincide '\$\(|`' "$cmd" && deny "sustitución de comandos"
coincide '--output' "$cmd" && deny "--output escribe en disco"
coincide '(^|[^[:alnum:]_./-])(tee|xargs|rm|mv|cp|ln|chmod|chown|touch|mkdir|rmdir|truncate|dd|kill|killall|sudo|eval|exec|source|env|export|nohup|open|osascript|psql|mysql|createdb|dropdb|perl|ruby|sh|bash|zsh|pytest)([[:space:]]|$)' "$cmd" \
  && deny "binario no permitido"

# --- Revisión por comando -----------------------------------------------------
# Los comandos unidos con && ; || se validan uno por uno contra la lista blanca.
# Dentro de un comando, las etapas después de un | solo pueden ser filtros.
# Los separadores entre comillas (por ejemplo, grep -E "a|b") no cuentan.
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

# pip: solo `list`, con flags opcionales (--outdated, --format=…).
check_pip() {
  local arg
  [ "${1:-}" = "list" ] || deny "pip ${1:-<vacío>} no está permitido (solo 'pip list [--outdated]')"
  shift
  for arg in "$@"; do
    case "$arg" in
      -i|--index-url|--extra-index-url|--find-links|-f|--index-url=*|--extra-index-url=*|--find-links=*)
        deny "pip list con índice alternativo no está permitido ('$arg')" ;;
      -*) ;;
      *) deny "pip list no admite argumentos posicionales ('$arg')" ;;
    esac
  done
}

# sed: solo `sed -n` con un rango o patrón seguido de p ('1,20p', '$p', '/x/p').
# Cualquier otro guion de sed puede escribir (w) o ejecutar (e).
check_sed() {
  local arg guion="" con_n=""
  for arg in "$@"; do
    case "$arg" in
      -n) con_n=1 ;;
      -E|-r) ;;
      -*) deny "sed solo admite -n, -E y -r ('$arg')" ;;
      *) if [ -z "$guion" ]; then guion="$arg"; fi ;;
    esac
  done
  [ -n "$con_n" ] || deny "sed solo se permite como 'sed -n'"
  guion="${guion//\'/}"; guion="${guion//\"/}"
  [[ "$guion" =~ ^((([0-9]+|\$)(,([0-9]+|\$))?)|(/[^/]*/(,/[^/]*/)?))p$ ]] \
    || deny "sed -n solo con un rango o patrón seguido de p, como '1,20p' o '/texto/p'"
}

# Binarios permitidos en cualquier etapa (primera o tras un pipe).
is_filter() {
  case "$1" in
    head|tail|grep|rg|wc|cat|sort|uniq|cut|tr|nl|column|awk|sed) return 0 ;;
    *) return 1 ;;
  esac
}

# Opciones que escriben o ejecutan, por binario.
check_readonly_opts() {
  local first="$1" seg="$2"
  shift 2
  case "$first" in
    find)
      coincide '-(delete|exec|execdir|ok|okdir|fprint0?|fprintf|fls)([[:space:]]|$)' "$seg" \
        && deny "find con acción de escritura o ejecución" ;;
    sed)
      check_sed "$@" ;;
    sort)
      coincide '(^|[[:space:]])(-o|--output)' "$seg" && deny "sort -o escribe en disco" ;;
    awk)
      coincide '(^|[[:space:]])(-i|-f|--file|--include)' "$seg" && deny "awk con -i o -f"
      coincide 'system|getline|\|' "$seg" && deny "awk con system, getline o un pipe puede ejecutar comandos" ;;
    rg)
      coincide '(^|[[:space:]])--pre([[:space:]=]|$)' "$seg" && deny "rg --pre ejecuta un programa" ;;
    tree)
      coincide '(^|[[:space:]])-o([[:space:]]|$)' "$seg" && deny "tree -o escribe en disco" ;;
  esac
  case "$first" in
    grep|rg)
      coincide '(^|[[:space:]])(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive|--dereference-recursive)([[:space:]]|$)' "$seg" \
        && coincide '(^|[[:space:]])(/|~|\.\./|\.\.([[:space:]]|$)|\$HOME)' "$seg" \
        && deny "búsqueda recursiva fuera del proyecto" ;;
  esac
  return 0
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
      is_filter "$first" || deny "'$first' no es un filtro de salida permitido tras un pipe"
      check_readonly_opts "$first" "$seg" "$@"
      continue
    fi

    if es_de "$first" pip || es_de "$first" pip3; then
      check_pip "$@"
    elif es_de "$first" python || es_de "$first" python3; then
      [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip" ] || deny "python solo se permite como 'python -m pip list [--outdated]'"
      shift 2
      check_pip "$@"
    elif [ "$first" = "git" ]; then
      [ "${1:-}" = "--no-pager" ] && shift
      case "${1:-}" in
        ls-files|log|show|grep|status|diff|blame|rev-parse|cat-file|ls-tree) ;;
        *) deny "git ${1:-<vacío>} no es de solo lectura o requiere red" ;;
      esac
      coincide '(^|[[:space:]])(--ext-diff|-O[^[:space:]]*|--open-files-in-pager[^[:space:]]*)([[:space:]]|$)' "$seg" \
        && deny "git con --ext-diff u -O ejecuta un programa externo"
    else
      case "$first" in
        ls|find|file|stat|tree|diff|cmp|strings|basename|dirname|realpath|pwd|date|which|type|true|test|\[|md5|shasum)
          check_readonly_opts "$first" "$seg" "$@" ;;
        *)
          is_filter "$first" || deny "'$first' no está en la lista blanca del security-auditor"
          check_readonly_opts "$first" "$seg" "$@" ;;
      esac
    fi
  done
done

# Si no se revisó ningún comando, algo falló al partir la entrada: se bloquea.
[ "$evaluados" -gt 0 ] || deny "no se pudo analizar el comando"
exit 0
