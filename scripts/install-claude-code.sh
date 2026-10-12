#!/usr/bin/env bash
# Installs Claude Code with the official native installer and checks PATH.
set -euo pipefail

if command -v claude >/dev/null 2>&1; then
  echo "Claude Code is already installed: $(claude --version)"
  echo "Run 'claude update' to get the latest version."
  exit 0
fi

case "$(uname -s)" in
  Darwin|Linux) ;;
  *)
    echo "Unsupported OS. On Windows, run in PowerShell:" >&2
    echo "  irm https://claude.ai/install.ps1 | iex" >&2
    exit 1
    ;;
esac

command -v curl >/dev/null 2>&1 || { echo "curl is required." >&2; exit 1; }

echo "Installing Claude Code..."
curl -fsSL https://claude.ai/install.sh | bash

bin_dir="$HOME/.local/bin"
if ! command -v claude >/dev/null 2>&1; then
  case "$(basename "${SHELL:-bash}")" in
    zsh)  rc="$HOME/.zshrc" ;;
    fish) rc="$HOME/.config/fish/config.fish" ;;
    *)    rc="$HOME/.bashrc" ;;
  esac
  echo
  echo "'claude' isn't on your PATH yet. Add this to $rc and restart your terminal:"
  if [[ "$rc" == *config.fish ]]; then
    echo "  fish_add_path $bin_dir"
  else
    echo "  export PATH=\"$bin_dir:\$PATH\""
  fi
  exit 0
fi

echo
echo "Installed: $(claude --version)"
echo "Next: cd into a project and run 'claude' to sign in."
