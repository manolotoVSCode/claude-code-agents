# Limitaciones

Estos agentes y hooks son un freno, no un entorno aislado. Reducen la probabilidad de que un agente haga algo que no debe, pero no la eliminan, y no sustituyen permisos reales, protecciones del servidor ni revisión humana. Léelo antes de confiarles algo que importe.

## Cómo deciden los hooks

- Los hooks de shell (`test-runner-bash.sh`, `security-auditor-bash.sh`, `explorer-readonly-bash.sh`) comparan el comando con una lista de binarios y opciones mediante expresiones regulares. Un comando que no anticipé puede pasar si está formado solo por piezas permitidas.
- `guardian-sql.py` clasifica el SQL con expresiones regulares y un lexer mínimo. Lo que el lexer no modela (comentarios de bloque, identificadores entre comillas dobles, literales con dólar, cadenas `E'…'`) pierde la vía VERDE y pasa a requerir aprobación, pero la frontera entre ÁMBAR y ROJO sigue siendo heurística: un `DELETE ... WHERE true` tiene WHERE y queda en ÁMBAR.
- Un ensayo `BEGIN ... ROLLBACK` se clasifica VERDE y se ejecuta de verdad. El ROLLBACK no deshace efectos fuera de la transacción: triggers que llaman a servicios externos, secuencias o notificaciones. El hook busca algunas de esas palabras en el texto del SQL, pero no puede ver lo que hace un trigger.
- `guardian-merge.py` revisa `gh pr merge`, `git push` a ramas protegidas y la ruta de merge de `gh api`. No ve merges hechos desde la web, desde otras herramientas, desde scripts ni con alias de shell. Configura además la protección de ramas en tu servidor de git.

## Lo que los hooks no vigilan

- **Solo vigilan la herramienta en la que están registrados.** Los hooks de los agentes miran Bash; Read, Grep, Glob, Write y Edit no pasan por ellos.
- **test-runner puede escribir código que luego se ejecuta.** Puede crear un `test_*.py` y correrlo con pytest, y ese archivo puede hacer cualquier cosa. La regla de escribir solo tests está en el texto del agente, no en un hook.
- **explorer y security-auditor pueden leer cualquier archivo** al que tenga acceso tu usuario, también fuera del proyecto, con Read o con `cat`. El hook del auditor solo bloquea las búsquedas recursivas que salen del proyecto.
- **La configuración de git del repositorio** (`diff.external`, filtros, `textconv`) puede ejecutar programas al correr `git diff` o `git log`. Los hooks bloquean `--ext-diff`, pero no revisan esa configuración.

## El guardián

- **"Solo el guardián escribe aprobaciones" es una regla del texto, no algo que los hooks comprueben.** El agente principal también puede escribir un archivo en `.claude/guardian/aprobaciones/` con Write o con Bash, y el hook lo aceptaría. Según la documentación de hooks, la entrada de un hook incluye `agent_type` cuando corre dentro de un subagente, así que un hook podría restringir quién escribe ahí; este repositorio no lo implementa.
- **La aprobación humana de un SQL ROJO es texto que el guardián recibe del agente principal.** El guardián no puede comprobar que lo escribió una persona.
- **El propio guardián tiene Bash sin restricciones** y acceso a las herramientas de base de datos que le des. Su contención es su texto.
- Cuando un hook de guardian aprueba, responde `permissionDecision: "allow"`. Según la documentación, eso omite la pregunta de permiso, aunque las reglas `deny` y `ask` de tu configuración se siguen evaluando.

## Cuándo un hook no bloquea

Según la documentación de hooks de Claude Code:

- Solo el código de salida 2 (o una decisión `deny` en JSON) bloquea. Cualquier otro código distinto de cero es un error que no bloquea: la acción sigue.
- Un hook que se queda sin tiempo no bloquea la acción.
- Por eso, si falta `python3`, los hooks de guardian no arrancan y no bloquean nada. Los hooks de shell sí bloquean si falta `jq` o si `grep` falla, y no dependen de archivos temporales; las pruebas lo cubren.
- La documentación describe `"onFailure": "block"` para que una falla del hook bloquee, desde Claude Code 2.1.295. Este repositorio no lo usa ni lo probé.
- Los hooks del encabezado de un agente de proyecto no corren hasta que confías en la carpeta del proyecto. Mientras tanto, el agente funciona con Bash sin restricción.
- Instalados como plugin, los agentes pierden los hooks de su encabezado: Claude Code ignora ese campo en los agentes de plugins. Por eso aquí la instalación es manual.

## Qué está probado y qué no

- Las pruebas de `tests/` alimentan cada hook con el JSON que recibiría por stdin y comprueban su decisión. Corren en macOS con bash 3.2.57 y Python 3.9.6. No las corrí en Linux ni en Windows.
- No probé los hooks dentro de una sesión real de Claude Code: las pruebas los ejecutan directamente.
- No probé `install.sh --usuario` completo; sí probé el modo `--proyecto` y la sustitución de ruta que hace el modo usuario.
- No hay ninguna medición de cuántos errores evitan estos agentes en la práctica.
