# Makefile for markdown-helper -- structured section-by-section markdown
# authoring MCP for AI agents.
#
# Idempotent install pattern, mirrors ~/src/utils/runner/Makefile. Installs
# to ~/.markdown-helper/ and registers the MCP server with opencode and
# Claude Code.

SHELL := /bin/bash
.ONESHELL:

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT          := $(shell pwd)
DEST          := $(HOME)/.markdown-helper
MCP_DIR       := $(ROOT)/mcp
MCP_DEST      := $(DEST)/mcp
CORE_DEST     := $(DEST)/core
DOCS_DEST     := $(DEST)/docs

OPENCODE_CONFIG := $(HOME)/.config/opencode/opencode.json
CLAUDE_CONFIG   := $(HOME)/.claude.json

# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

.PHONY: all build build-mcp clean install uninstall help \
        setup setup-macos setup-linux \
        install-files install-pydeps \
        register-opencode unregister-opencode \
        register-claude unregister-claude \
        check-deps

all: build

help:
	@echo "Usage: make [target]"
	@echo ""
	@echo "Targets:"
	@echo "  setup        - Install system dependencies (jq, node, python3, pip, rsync) on macOS or Linux (idempotent)"
	@echo "  build        - Build the MCP server (npm install + tsc)"
	@echo "  install      - Install to $(DEST), register MCP with opencode + Claude (idempotent)"
	@echo "  uninstall    - Remove install + unregister"
	@echo "  clean        - Remove built MCP artifacts"
	@echo "  help         - Show this help"

# ---------------------------------------------------------------------------
# Setup: install system dependencies (idempotent)
# ---------------------------------------------------------------------------
#
# Only installs what's missing. Safe to run repeatedly. Required deps:
#   jq, npm (node), python3, pip, rsync
#
# macOS:  uses Homebrew (user-local, no sudo). Installs brew if missing.
# Linux:  uses apt / dnf / pacman with sudo (system package managers are
#         the right tool for system tooling on Linux).

setup:
	@UNAME="$$(uname -s)"; \
	case "$$UNAME" in \
		Darwin) $(MAKE) --no-print-directory setup-macos ;; \
		Linux)  $(MAKE) --no-print-directory setup-linux ;; \
		*) echo "ERROR: unsupported OS: $$UNAME (need Darwin or Linux)"; exit 1 ;; \
	esac
	@$(MAKE) --no-print-directory install-pydeps
	@echo ""
	@echo "+ setup complete -- run 'make install' next"

setup-macos:
	@echo "> Checking macOS dependencies (Homebrew)"
	@if ! command -v brew >/dev/null 2>&1; then \
		echo "  brew not found -- installing Homebrew (user-local, no sudo)"; \
		/bin/bash -c "$$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"; \
	else \
		echo "  brew: already installed"; \
	fi
	@MISSING=""; \
	for pkg in jq node python3 rsync; do \
		bin="$$pkg"; \
		[ "$$pkg" = "node" ] && bin="node"; \
		if command -v "$$bin" >/dev/null 2>&1; then \
			echo "  $$pkg: already installed"; \
		else \
			MISSING="$$MISSING $$pkg"; \
		fi; \
	done; \
	if [ -n "$$MISSING" ]; then \
		echo "  installing:$$MISSING"; \
		brew install $$MISSING; \
	fi
	@command -v pip3 >/dev/null 2>&1 || command -v pip >/dev/null 2>&1 || { \
		echo "ERROR: pip not found after python3 install"; exit 1; }

setup-linux:
	@echo "> Checking Linux dependencies"
	@if command -v apt-get >/dev/null 2>&1; then \
		PM="apt"; \
	elif command -v dnf >/dev/null 2>&1; then \
		PM="dnf"; \
	elif command -v pacman >/dev/null 2>&1; then \
		PM="pacman"; \
	else \
		echo "ERROR: no supported package manager found (need apt, dnf, or pacman)"; \
		exit 1; \
	fi; \
	echo "  package manager: $$PM"; \
	MISSING=""; \
	command -v jq      >/dev/null 2>&1 && echo "  jq: already installed"      || MISSING="$$MISSING jq"; \
	command -v npm     >/dev/null 2>&1 && echo "  npm: already installed"     || MISSING="$$MISSING npm"; \
	command -v python3 >/dev/null 2>&1 && echo "  python3: already installed" || MISSING="$$MISSING python3"; \
	command -v pip3    >/dev/null 2>&1 || command -v pip >/dev/null 2>&1 && echo "  pip: already installed" || MISSING="$$MISSING pip"; \
	command -v rsync   >/dev/null 2>&1 && echo "  rsync: already installed"   || MISSING="$$MISSING rsync"; \
	if [ -z "$$MISSING" ]; then \
		echo "+ all system dependencies already installed"; \
		exit 0; \
	fi; \
	echo "  installing (sudo):$$MISSING"; \
	case "$$PM" in \
		apt) \
			PKGS=""; \
			for m in $$MISSING; do \
				case "$$m" in \
					npm)     PKGS="$$PKGS nodejs npm" ;; \
					pip)     PKGS="$$PKGS python3-pip" ;; \
					python3) PKGS="$$PKGS python3" ;; \
					*)       PKGS="$$PKGS $$m" ;; \
				esac; \
			done; \
			sudo apt-get update && sudo apt-get install -y $$PKGS ;; \
		dnf) \
			PKGS=""; \
			for m in $$MISSING; do \
				case "$$m" in \
					npm)     PKGS="$$PKGS nodejs npm" ;; \
					pip)     PKGS="$$PKGS python3-pip" ;; \
					python3) PKGS="$$PKGS python3" ;; \
					*)       PKGS="$$PKGS $$m" ;; \
				esac; \
			done; \
			sudo dnf install -y $$PKGS ;; \
		pacman) \
			PKGS=""; \
			for m in $$MISSING; do \
				case "$$m" in \
					npm)     PKGS="$$PKGS nodejs npm" ;; \
					pip)     PKGS="$$PKGS python-pip" ;; \
					python3) PKGS="$$PKGS python" ;; \
					*)       PKGS="$$PKGS $$m" ;; \
				esac; \
			done; \
			sudo pacman -S --needed --noconfirm $$PKGS ;; \
	esac

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

check-deps:
	@command -v jq >/dev/null 2>&1 || { echo "ERROR: jq not installed (run 'make setup')"; exit 1; }
	@command -v npm >/dev/null 2>&1 || { echo "ERROR: npm not installed (run 'make setup')"; exit 1; }
	@command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not installed (run 'make setup')"; exit 1; }
	@command -v pip3 >/dev/null 2>&1 || command -v pip >/dev/null 2>&1 || { echo "ERROR: pip not installed (run 'make setup')"; exit 1; }
	@command -v rsync >/dev/null 2>&1 || { echo "ERROR: rsync not installed (run 'make setup')"; exit 1; }

# Python deps. mistletoe is a hard requirement -- the CommonMark parser
# replaces the hand-rolled regex scanner in core/md_helper_core.py.
# --break-system-packages is required on modern Debian/Ubuntu where pip
# refuses to touch the system Python without it; this is the right move
# for a system-wide tool.
install-pydeps:
	@echo "> Installing Python dependencies (mistletoe)"
	@if command -v pip3 >/dev/null 2>&1; then PIP=pip3; \
	elif command -v pip >/dev/null 2>&1; then PIP=pip; \
	else echo "ERROR: neither pip3 nor pip found"; exit 1; fi; \
	if python3 -c "import mistletoe" 2>/dev/null; then \
		echo "+ mistletoe already installed"; \
		exit 0; \
	fi; \
	$$PIP install --user --quiet mistletoe >/dev/null 2>&1 || \
		$$PIP install --break-system-packages --quiet mistletoe >/dev/null 2>&1 || \
		$$PIP install --quiet mistletoe >/dev/null 2>&1 || \
		{ echo "ERROR: failed to install mistletoe"; exit 1; }; \
	python3 -c "import mistletoe" 2>/dev/null || \
		{ echo "ERROR: mistletoe not importable after install"; exit 1; }; \
	echo "+ mistletoe installed"

build: check-deps install-pydeps build-mcp

build-mcp:
	@echo "> Building markdown-helper MCP server"
	@if [ ! -f "$(MCP_DIR)/package.json" ]; then \
		echo "ERROR: $(MCP_DIR)/package.json not found"; \
		exit 1; \
	fi
	@# Prefer `npm ci` for reproducible installs from package-lock.json.
	@# Falls back to `npm install` if the lockfile is missing (shouldn't
	@# happen in committed state, but keeps the build robust).
	@if [ -f "$(MCP_DIR)/package-lock.json" ]; then \
		cd "$(MCP_DIR)" && npm ci --no-fund --no-audit >/dev/null 2>&1; \
	else \
		cd "$(MCP_DIR)" && npm install --no-fund --no-audit >/dev/null 2>&1; \
	fi
	@cd "$(MCP_DIR)" && npm run build >/dev/null 2>&1
	@echo "+ MCP server built at $(MCP_DIR)/dist/"

clean:
	@echo "> Cleaning markdown-helper MCP build artifacts"
	@rm -rf "$(MCP_DIR)/dist" "$(MCP_DIR)/node_modules"
	@echo "+ Clean complete"

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

install: build install-files register-opencode register-claude
	@echo ""
	@echo "+ markdown-helper installed to $(DEST)"
	@echo "  Restart opencode/claude to pick up the new MCP server."

install-files:
	@echo "> Installing markdown-helper files to $(DEST)"
	@mkdir -p "$(CORE_DEST)" "$(DOCS_DEST)" "$(MCP_DEST)"
	@rsync -a --delete "$(ROOT)/core/" "$(CORE_DEST)/"
	@rsync -a --delete "$(ROOT)/docs/" "$(DOCS_DEST)/"
	@rsync -a --delete "$(MCP_DIR)/dist/"         "$(MCP_DEST)/dist/"
	@rsync -a --delete "$(MCP_DIR)/node_modules/" "$(MCP_DEST)/node_modules/"
	@cp "$(MCP_DIR)/package.json" "$(MCP_DEST)/"
	@echo "+ Files synced"

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

uninstall: unregister-opencode unregister-claude
	@echo "> Removing markdown-helper install"
	@rm -rf "$(DEST)"
	@echo "+ Removed $(DEST)"
	@echo ""
	@echo "Note: per-project document dirs (<project>/.markdown-helper/<docId>/) are NOT touched."

# ---------------------------------------------------------------------------
# opencode registration
# ---------------------------------------------------------------------------

register-opencode:
	@echo "> Registering markdown-helper MCP with opencode"
	@if [ ! -f "$(OPENCODE_CONFIG)" ]; then \
		mkdir -p "$$(dirname "$(OPENCODE_CONFIG)")"; \
		echo '{"$$schema": "https://opencode.ai/config.json"}' > "$(OPENCODE_CONFIG)"; \
	fi
	@if jq -e '.mcp["markdown-helper"]' "$(OPENCODE_CONFIG)" >/dev/null 2>&1; then \
		echo "+ markdown-helper MCP already registered, updating..."; \
	fi
	@MCP_CONFIG='{"type":"local","command":["node","$(MCP_DEST)/dist/index.js"],"enabled":true}'; \
	jq --argjson md "$$MCP_CONFIG" '.mcp["markdown-helper"] = $$md' "$(OPENCODE_CONFIG)" > "$(OPENCODE_CONFIG).tmp" && \
		mv "$(OPENCODE_CONFIG).tmp" "$(OPENCODE_CONFIG)"
	@echo "+ Registered markdown-helper MCP in $(OPENCODE_CONFIG)"

unregister-opencode:
	@echo "> Unregistering markdown-helper MCP from opencode"
	@if [ -f "$(OPENCODE_CONFIG)" ] && jq -e '.mcp["markdown-helper"]' "$(OPENCODE_CONFIG)" >/dev/null 2>&1; then \
		jq 'del(.mcp["markdown-helper"])' "$(OPENCODE_CONFIG)" > "$(OPENCODE_CONFIG).tmp" && \
			mv "$(OPENCODE_CONFIG).tmp" "$(OPENCODE_CONFIG)"; \
		echo "+ Removed markdown-helper MCP from $(OPENCODE_CONFIG)"; \
	else \
		echo "- markdown-helper MCP not found in opencode config"; \
	fi

# ---------------------------------------------------------------------------
# Claude Code registration
# ---------------------------------------------------------------------------

register-claude:
	@echo "> Registering markdown-helper MCP with Claude Code"
	@if [ ! -f "$(CLAUDE_CONFIG)" ]; then \
		echo '{}' > "$(CLAUDE_CONFIG)"; \
	fi
	@if jq -e '.mcpServers["markdown-helper"]' "$(CLAUDE_CONFIG)" >/dev/null 2>&1; then \
		echo "+ markdown-helper MCP already registered, updating..."; \
	fi
	@MCP_CONFIG='{"type":"stdio","command":"node","args":["$(MCP_DEST)/dist/index.js"]}'; \
	jq --argjson md "$$MCP_CONFIG" '.mcpServers["markdown-helper"] = $$md' "$(CLAUDE_CONFIG)" > "$(CLAUDE_CONFIG).tmp" && \
		mv "$(CLAUDE_CONFIG).tmp" "$(CLAUDE_CONFIG)"
	@echo "+ Registered markdown-helper MCP in $(CLAUDE_CONFIG)"

unregister-claude:
	@echo "> Unregistering markdown-helper MCP from Claude Code"
	@if [ -f "$(CLAUDE_CONFIG)" ] && jq -e '.mcpServers["markdown-helper"]' "$(CLAUDE_CONFIG)" >/dev/null 2>&1; then \
		jq 'del(.mcpServers["markdown-helper"])' "$(CLAUDE_CONFIG)" > "$(CLAUDE_CONFIG).tmp" && \
			mv "$(CLAUDE_CONFIG).tmp" "$(CLAUDE_CONFIG)"; \
		echo "+ Removed markdown-helper MCP from $(CLAUDE_CONFIG)"; \
	else \
		echo "- markdown-helper MCP not found in Claude config"; \
	fi
