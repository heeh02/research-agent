#!/bin/bash
# Research Agent Setup — installs all dependencies including codex-plugin-cc
set -e

echo "=== Research Agent Setup ==="
echo

# 1. Python dependencies
echo "[1/4] Installing Python package..."
pip install -e .
echo "✓ Python package installed"
echo

# 2. Codex CLI
echo "[2/4] Checking Codex CLI..."
if command -v codex &> /dev/null; then
    echo "✓ Codex CLI already installed: $(codex --version)"
else
    echo "Installing Codex CLI..."
    if command -v npm &> /dev/null; then
        npm install -g @openai/codex
        echo "✓ Codex CLI installed"
    elif command -v brew &> /dev/null; then
        brew install --cask codex
        echo "✓ Codex CLI installed via Homebrew"
    else
        echo "⚠ Cannot install Codex CLI: npm or brew not found"
        echo "  Install manually: npm install -g @openai/codex"
        echo "  Or: brew install --cask codex"
    fi
fi
echo

# 3. Codex authentication
echo "[3/4] Checking Codex authentication..."
echo "If not yet authenticated, run: codex login"
echo

# 4. Claude Code plugin
echo "[4/4] codex-plugin-cc setup"
echo "In your Claude Code session, run these commands:"
echo
echo "  /plugin marketplace add openai/codex-plugin-cc"
echo "  /plugin install codex@openai-codex"
echo "  /reload-plugins"
echo "  /codex:setup"
echo
echo "=== Setup Complete ==="
echo
echo "Quick start:"
echo "  python scripts/pipeline.py init 'My Research' -q 'Research question?'"
echo "  python scripts/pipeline.py status"
echo "  python scripts/pipeline.py run"
