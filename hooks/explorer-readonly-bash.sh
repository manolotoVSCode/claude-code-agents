#!/bin/bash
# Hook PreToolUse para el subagente `explorer`.
# Deja pasar solo comandos de Bash de lectura; cualquier otro se bloquea con
# exit 2.
#
# Permitido: ls cat head tail wc grep rg tree uniq cut tr echo printf pwd file
#   stat du df jq which type date basename dirname realpath diff cmp md5 shasum
#   strings nl column true test, find sin acciones, sed -n con un rango o
#   patrón seguido de p, sort sin -o, awk sin system ni getline, y git de
#   lectura (log show diff status blame ls-files rev-parse shortlog grep
#   describe reflog cat-file ls-tree count-objects name-rev, más branch, tag y
#   remote solo en sus formas de listar).
#
# Bloqueado: redirecciones de salida, sustitución de comandos, --output,
# instaladores, intérpretes, binarios que escriben o usan la red, y opciones
# que escriben o ejecutan (rg --pre, tree -o, git --ext-diff, git grep -O).
#
# Entrada: el JSON del hook por stdin ({ tool_name, tool_input: { command } }).
# Requiere jq. Sin jq, o ante un error interno, bloquea.
set -u
set -f   # sin expansión de comodines al partir el comando en palabras

if ! command -v jq >/dev/null 2>&1; then
  echo "explorer: falta jq; comando bloqueado por precaución" >&2
  exit 2
fi
input=$(cat)
if ! printf '%s' "$input" | jq . >/dev/null 2>&1; then
  echo "explorer: el input del hook no se pudo leer como JSON; comando bloqueado por precaución" >&2
  exit 2
fi
cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // empty') || exit 2
[ -z "$cmd" ] && exit 0   # sin tool_input.command: no es un evento Bash, no bloquear

deny() {
  echo "explorer (solo lectura): comando bloqueado — $1" >&2
  echo "Comando: $cmd" >&2
  exit 2
}

# coincide <regex-extendida> <texto>: 0 si coincide, 1 si no.
# Cualquier otro resultado de grep es un error y bloquea (falla cerrada).
coincide() {
  local r
  printf '%s\n' "$2" | grep -Eq -- "$1"
  r=$?
  [ "$r" -le 1 ] || deny "error interno al evaluar el comando"
  return "$r"
}

# --- Bloqueos globales --------------------------------------------------------
case "$cmd" in
  *'>'*) deny "redirección de salida (>)" ;;
esac
coincide '\$\(|`|<\(' "$cmd" && deny "sustitución de comandos"
coincide '--output' "$cmd" && deny "--output escribe en disco"
coincide '(^|[^[:alnum:]_./-])(tee|xargs|rm|mv|cp|ln|chmod|chown|touch|mkdir|rmdir|truncate|dd|npm|npx|pnpm|yarn|node|bun|python3?|perl|ruby|sh|bash|zsh|curl|wget|kill|killall|sudo|eval|exec|source|env|export|nohup|open|osascript|psql|mysql|createdb|dropdb)([[:space:]]|$)' "$cmd" \
  && deny "binario no permitido"

# --- Revisión por segmento (se parte en | && ; ||) ---------------------------
# Los separadores entre comillas (por ejemplo, un patrón grep -E "a|b") no
# cuentan como separadores.
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

# git branch, tag y remote escriben si reciben un nombre: solo se permiten
# sus formas de listar.
check_git_listar() {
  local sub="$1" arg
  shift
  case "$sub" in
    branch)
      for arg in "$@"; do
        case "$arg" in
          -a|-r|-v|-vv|--all|--remotes|--list|-l|--show-current|--verbose|--no-color|--color) ;;
          --contains|--no-contains|--merged|--no-merged|--points-at|--sort=*|--format=*|--contains=*|--merged=*) ;;
          *) deny "git branch solo se permite para listar ('$arg')" ;;
        esac
      done ;;
    tag)
      [ "$#" -eq 0 ] && return 0
      case "${1:-}" in
        -l|--list) ;;
        *) deny "git tag solo se permite para listar (git tag, git tag -l)" ;;
      esac ;;
    remote)
      [ "$#" -eq 0 ] && return 0
      [ "$#" -eq 1 ] && [ "$1" = "-v" ] && return 0
      deny "git remote solo se permite como 'git remote' o 'git remote -v'" ;;
  esac
}

IFS=$'\n'
segmentos=( $(split_unquoted "$cmd" ';&|') )
unset IFS
evaluados=0

for seg in ${segmentos[@]+"${segmentos[@]}"}; do
  seg="${seg#"${seg%%[![:space:](]*}"}"   # quita espacios y '(' iniciales
  [ -z "${seg// /}" ] && continue
  evaluados=$((evaluados + 1))

  set -- $seg
  first="$1"; shift
  case "$first" in
    git)
      [ "${1:-}" = "-C" ] && shift 2
      [ "${1:-}" = "--no-pager" ] && shift
      sub="${1:-}"
      [ "$#" -gt 0 ] && shift
      case "$sub" in
        log|show|diff|status|blame|ls-files|rev-parse|shortlog|grep|describe|reflog|cat-file|ls-tree|count-objects|name-rev) ;;
        branch|tag|remote) check_git_listar "$sub" "$@" ;;
        *) deny "git $sub no es de solo lectura" ;;
      esac
      coincide '(^|[[:space:]])(--ext-diff|-O[^[:space:]]*|--open-files-in-pager[^[:space:]]*)([[:space:]]|$)' "$seg" \
        && deny "git con --ext-diff u -O ejecuta un programa externo"
      ;;
    find)
      coincide '-(delete|exec|execdir|ok|okdir|fprint0?|fprintf|fls)([[:space:]]|$)' "$seg" \
        && deny "find con acción de escritura o ejecución"
      ;;
    sed)
      check_sed "$@"
      ;;
    sort)
      coincide '(^|[[:space:]])(-o|--output)' "$seg" && deny "sort -o escribe en disco"
      ;;
    awk)
      coincide '(^|[[:space:]])(-i|-f|--file|--include)' "$seg" && deny "awk con -i o -f"
      coincide 'system|getline|\|' "$seg" && deny "awk con system, getline o un pipe puede ejecutar comandos"
      ;;
    rg)
      coincide '(^|[[:space:]])--pre([[:space:]=]|$)' "$seg" && deny "rg --pre ejecuta un programa"
      ;;
    tree)
      coincide '(^|[[:space:]])-o([[:space:]]|$)' "$seg" && deny "tree -o escribe en disco"
      ;;
    ls|cat|head|tail|wc|grep|uniq|cut|tr|echo|printf|pwd|file|stat|du|df|jq|which|type|date|basename|dirname|realpath|diff|cmp|md5|shasum|strings|nl|column|true|test|\[) ;;
    *) deny "'$first' no está en la lista de comandos de solo lectura" ;;
  esac
done

# Si no se revisó ningún comando, algo falló al partir la entrada: se bloquea.
[ "$evaluados" -gt 0 ] || deny "no se pudo analizar el comando"
exit 0
