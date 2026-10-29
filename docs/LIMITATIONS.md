# Limitations

These agents and hooks are a brake, not a sandbox. They reduce the chance that an agent does something it shouldn't, but they don't eliminate it, and they don't replace real permissions, server-side protections or human review. Read this before trusting them with anything that matters.

## How the hooks decide

- The shell hooks (`test-runner-bash.sh`, `security-auditor-bash.sh`, `explorer-readonly-bash.sh`) compare the command against a list of binaries and options using regular expressions. A command I didn't anticipate can get through if it's built only from allowed pieces.
- `guardian-sql.py` classifies SQL with regular expressions and a minimal lexer. Anything the lexer doesn't model (block comments, double-quoted identifiers, dollar-quoted literals, `E'…'` strings) loses the GREEN path and requires approval, but the line between AMBER and RED is still heuristic: a `DELETE ... WHERE true` has a WHERE and stays AMBER.
- A `BEGIN ... ROLLBACK` dry run is classified GREEN and actually runs. The ROLLBACK doesn't undo effects outside the transaction: triggers that call external services, sequences or notifications. The hook looks for some of those words in the SQL text, but it can't see what a trigger does.
- `guardian-merge.py` checks `gh pr merge`, `git push` to protected branches and the merge path of `gh api`. It doesn't see merges made from the web, from other tools, from scripts or through shell aliases. Also configure branch protection on your git server.

## What the hooks don't watch

- **They only watch the tool they're registered on.** The agents' hooks watch Bash; Read, Grep, Glob, Write and Edit don't go through them.
- **test-runner can write code that then runs.** It can create a `test_*.py` and run it with pytest, and that file can do anything. The rule of writing only tests lives in the agent's prompt, not in a hook.
- **explorer and security-auditor can read any file** your user can access, including outside the project, with Read or with `cat`. The auditor's hook only blocks recursive searches that leave the project.
- **The repository's git configuration** (`diff.external`, filters, `textconv`) can run programs when you run `git diff` or `git log`. The hooks block `--ext-diff`, but they don't inspect that configuration.

## The guardian

- **"Only the guardian writes approvals" is a rule in the prompt, not something the hooks check.** The main agent can also write a file in `.claude/guardian/aprobaciones/` with Write or Bash, and the hook would accept it. According to the hooks documentation, a hook's input includes `agent_type` when it runs inside a subagent, so a hook could restrict who writes there; this repository doesn't implement that.
- **Human approval of a RED statement is text the guardian receives from the main agent.** The guardian can't verify that a person wrote it.
- **The guardian itself has unrestricted Bash** and access to whatever database tools you give it. What contains it is its prompt.
- When a guardian hook approves, it returns `permissionDecision: "allow"`. According to the documentation, that skips the permission prompt, although the `deny` and `ask` rules in your configuration are still evaluated.

## When a hook doesn't block

According to the Claude Code hooks documentation:

- Only exit code 2 (or a `deny` decision in JSON) blocks. Any other non-zero code is a non-blocking error: the action goes ahead.
- A hook that times out doesn't block the action.
- So if `python3` is missing, the guardian hooks don't start and don't block anything. The shell hooks do block if `jq` is missing or `grep` fails, and they don't depend on temporary files; the tests cover this.
- The documentation describes `"onFailure": "block"` to make a hook failure block, from Claude Code 2.1.295. This repository doesn't use it, and I haven't tested it.
- Hooks in a project agent's frontmatter don't run until you trust the project folder. Until then, the agent runs with unrestricted Bash.
- Installed as a plugin, the agents lose the hooks in their frontmatter: Claude Code ignores that field in plugin agents. That's why installation here is manual.

## What's tested and what isn't

- The tests in `tests/` feed each hook the JSON it would receive on stdin and check its decision. They run on macOS with bash 3.2.57 and Python 3.9.6. I haven't run them on Linux or Windows.
- I haven't tested the hooks inside a real Claude Code session: the tests run them directly.
- I haven't tested `install.sh --usuario` end to end; I did test `--proyecto` mode and the path substitution the user mode performs.
- There's no measurement of how many mistakes these agents prevent in practice.
