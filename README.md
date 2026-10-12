# Claude Code terminal setup

Get Claude Code running in your terminal, plus a starter config you can copy into any project.

## 1. Install

**macOS / Linux / WSL**

```bash
./scripts/install-claude-code.sh
```

The script runs the official native installer (`curl -fsSL https://claude.ai/install.sh | bash`), checks that `claude` is on your `PATH`, and tells you what line to add to your shell profile if it isn't.

**Windows (PowerShell)**

```powershell
irm https://claude.ai/install.ps1 | iex
```

**Alternatives**

```bash
brew install --cask claude-code          # Homebrew
npm install -g @anthropic-ai/claude-code # npm (needs Node.js 18+); don't use sudo
```

## 2. Sign in

```bash
cd your-project
claude
```

The first run opens a browser to log in with your Claude account (Pro, Max, Team or Enterprise) or an Anthropic Console account. To use an API key instead, export `ANTHROPIC_API_KEY` before running `claude`.

## 3. Check the install

```bash
claude --version
claude doctor
```

## 4. Terminal quality-of-life

Run these inside a `claude` session:

| Command | What it does |
| --- | --- |
| `/terminal-setup` | Binds Shift+Enter to insert a newline (iTerm2, VS Code, and others) |
| `/config` | Theme, model, notifications, editor mode (vim) |
| `/statusline` | Build a custom status line |
| `/init` | Generate a `CLAUDE.md` for the current repo |
| `/permissions` | Review allowed and denied tools |

Handy shortcuts: `Esc` interrupts, `Esc Esc` rewinds, `Shift+Tab` cycles permission modes, `@` mentions a file, `!` runs a shell command, `#` saves a memory.

## 5. Use the starter config

Copy these into a project to start with sensible defaults:

- [`.claude/settings.json`](.claude/settings.json): allows common read-only commands without prompting and blocks reads of secrets (`.env`, keys)
- [`CLAUDE.md`](CLAUDE.md): a template for project instructions Claude reads every session

```bash
cp -r .claude CLAUDE.md /path/to/your-project/
```

Personal overrides go in `.claude/settings.local.json` (git-ignored) or `~/.claude/settings.json` for all projects.

## Updating

The native install updates itself. Run `claude update` to update right away.
