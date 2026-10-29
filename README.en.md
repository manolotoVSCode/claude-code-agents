# Agents for Claude Code

[Español](README.md)

Five [Claude Code](https://code.claude.com/docs/en/sub-agents) subagents, with the hooks that restrict what they can run. They're generic versions of the ones I use in my projects.

> Personal project, not affiliated with Anthropic.

| Agent | What it does | How it's enforced |
|---|---|---|
| `guardian` | Reviews and dry-runs write SQL and decides whether a PR can be merged into the main branch. Writes one-use approvals bound to the exact SQL hash or the PR number. | `guardian-sql.py` and `guardian-merge.py`, registered in `settings.json`, block write SQL and merges that have no approval. |
| `code-reviewer` | Reviews changes read-only and returns findings with severity and `file:line`, fixing nothing. | Has no Bash, Write or Edit (`disallowedTools`). |
| `test-runner` | Writes and runs pytest tests and reports failures with their likely cause. | A hook in its frontmatter limits Bash to pytest and `pip list` and `pip check`. |
| `security-auditor` | Looks for exposed secrets and outdated dependencies, and never quotes a full secret. | A hook in its frontmatter limits Bash to read-only commands and `pip list`. No Write or Edit. |
| `explorer` | Answers questions about code, docs and git history, citing its evidence. | A hook in its frontmatter limits Bash to read-only commands. No Write or Edit. |

The hooks are a brake, not a sandbox: they classify commands and SQL with regular expressions. Before using them, read [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

## Installation

Requirements:

- `jq`, for the shell hooks. Without jq, those hooks block every Bash command from their agent.
- Python 3.9 or later, for the guardian hooks. Without Python, those hooks don't start and **block nothing**.
- `gh`, logged in, for `guardian-merge.py`. Without gh, the hook denies every merge.

```bash
git clone https://github.com/manolotoVSCode/claude-code-agents.git
```

In a project (agents go to `.claude/agents/` and hooks to `.claude/hooks/`):

```bash
./claude-code-agents/install.sh --proyecto /path/to/your/project
```

For all your projects (in `~/.claude/`):

```bash
./claude-code-agents/install.sh --usuario
```

Add `--idioma en` to install the English prompts. The script doesn't overwrite existing files without asking and doesn't modify any `settings.json`.

I deliberately didn't package this as a plugin: Claude Code ignores the `hooks` field in plugin agents, so test-runner, security-auditor and explorer would end up with unrestricted Bash and no warning.

### Turning on the guardian

The guardian hooks don't go in the agent's frontmatter, because they need to watch the main session, not the guardian. You register them in the project's `.claude/settings.json`:

1. Copy the blocks from [examples/settings.hooks.json](examples/settings.hooks.json) into the `hooks` key of your `settings.json`.
2. Replace `mcp__db__execute_sql|mcp__db__apply_migration` with the names of your MCP server's tools that run SQL. The format is `mcp__<server>__<tool>`.
3. Add those same tools to the `tools` line in `guardian.md` so the guardian can dry-run the SQL.
4. If you installed into `~/.claude/`, replace `${CLAUDE_PROJECT_DIR}/.claude/hooks/` with the absolute path of your `~/.claude/hooks/` folder.

Approvals, backups and the log live in the project's `.claude/guardian/`. Add that folder to your `.gitignore` if you don't want it versioned.

## Configuration

Environment variables the hooks read. You can set them in the `env` key of `settings.json`.

| Variable | Hook | Purpose | Default |
|---|---|---|---|
| `CLAUDE_AGENTS_VENV` | test-runner, security-auditor | Virtualenv folder, relative to the project. | `.venv` |
| `GUARDIAN_RAMAS` | guardian-merge | Protected branches, comma-separated. | `main,master` |
| `GUARDIAN_VENTANA_BLOQUEO` | guardian-merge | Hours when only urgent merges pass, for example `1-5 07:00-21:00` (ISO days, 1 = Monday). | no window |
| `GUARDIAN_ZONA` | guardian-merge | Time zone of the window, for example `America/New_York`. | the system's |
| `GUARDIAN_ESQUEMAS_PROTEGIDOS` | guardian-sql | Schemas whose mention in a write makes it RED. | `pg_catalog,information_schema` |

## How the guardian works

`guardian-sql.py` sorts each SQL statement into three levels:

- **GREEN (VERDE):** a read, or a `BEGIN ... ROLLBACK` dry run. It passes.
- **AMBER (ÁMBAR):** a recoverable write. It passes only if `.claude/guardian/aprobaciones/<sha256>.json` exists, where sha256 is the hash of the exact SQL.
- **RED (ROJO):** DROP, TRUNCATE, UPDATE or DELETE without WHERE, permissions, roles, disabling RLS or triggers. Besides the file, it requires the `aprobacion_humana` field.

When the approval is missing, the hook denies and tells the main agent to invoke the guardian with that SQL. The guardian dry-runs it, saves a backup and the reverse SQL, and writes the approval. When it's used, the hook renames it to `.usada`, so it works only once. Everything is logged to `.claude/guardian/registro.log`. On an internal error, the hook denies.

`guardian-merge.py` does the same for `gh pr merge <N>`: it requires the PR number, rejects `--admin`, requires complete, green CI and a `pr-<N>.json` file with a `merge` verdict, and consumes it. It also blocks a direct `git push` to the protected branches.

The hooks' messages, the approval fields and the Spanish prompts' identifiers are in Spanish; the English prompts explain them.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests feed each hook the JSON it would receive from Claude Code and check whether it lets the call through or blocks it. They use no network: `gh` is simulated. On macOS, with bash 3.2.57 and Python 3.9.6, all 49 pass.

## License

[MIT](LICENSE).
