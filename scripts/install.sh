#!/usr/bin/env bash
#
# install.sh - Selective installation for Claude Code prerequisites
#
# Usage:
#   ./scripts/install.sh             # Interactive mode
#   ./scripts/install.sh rtk         # Install specific tool
#   ./scripts/install.sh --all       # Install all tools
#   ./scripts/install.sh --missing   # Install only missing tools
#   ./scripts/install.sh --list      # List available tools
#
# Exit codes:
#   0 - Success
#   1 - Installation failed
#   2 - Script error
#

set -euo pipefail

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Available tools
AVAILABLE_TOOLS=(rtk codegraph uvx codebase-memory-mcp direnv)

# -----------------------------------------------------------------------------
# Platform detection
# -----------------------------------------------------------------------------

detect_platform() {
    local os=""
    local arch=""

    case "$(uname -s)" in
        Darwin)  os="macos" ;;
        Linux)   os="linux" ;;
        MINGW*|MSYS*|CYGWIN*) os="windows" ;;
        *)       os="unknown" ;;
    esac

    case "$(uname -m)" in
        x86_64)  arch="amd64" ;;
        aarch64|arm64) arch="arm64" ;;
        *)       arch="unknown" ;;
    esac

    echo "$os:$arch"
}

PLATFORM=$(detect_platform)
OS="${PLATFORM%%:*}"
ARCH="${PLATFORM##*:}"

# -----------------------------------------------------------------------------
# Utility functions
# -----------------------------------------------------------------------------

info() {
    echo -e "\033[1;34m[INFO]\033[0m $*"
}

success() {
    echo -e "\033[1;32m[OK]\033[0m $*"
}

warn() {
    echo -e "\033[1;33m[WARN]\033[0m $*"
}

error() {
    echo -e "\033[1;31m[ERROR]\033[0m $*" >&2
}

# Check if a command exists
command_exists() {
    command -v "$1" &>/dev/null
}

# Ask for user confirmation
confirm() {
    local prompt="${1:-Continue?}"
    local default="${2:-n}"

    if [[ "$default" == "y" ]]; then
        read -r -p "$prompt [Y/n] " response
        [[ -z "$response" || "$response" =~ ^[Yy] ]]
    else
        read -r -p "$prompt [y/N] " response
        [[ "$response" =~ ^[Yy] ]]
    fi
}

# -----------------------------------------------------------------------------
# Tool detection functions
# -----------------------------------------------------------------------------

is_rtk_installed() {
    if command_exists rtk; then
        # Check for rtk gain to distinguish from Rust Type Kit
        if rtk gain --help &>/dev/null 2>&1 || rtk gain &>/dev/null 2>&1; then
            return 0
        fi
    fi
    return 1
}

is_codegraph_installed() {
    command_exists codegraph
}

is_uvx_installed() {
    command_exists uvx || command_exists uv
}

is_codebase_memory_mcp_installed() {
    command_exists codebase-memory-mcp || npm list -g codebase-memory-mcp &>/dev/null 2>&1
}

is_direnv_installed() {
    command_exists direnv
}

# Check if a tool is installed
is_tool_installed() {
    local tool="$1"
    case "$tool" in
        rtk)                 is_rtk_installed ;;
        codegraph)           is_codegraph_installed ;;
        uvx)                 is_uvx_installed ;;
        codebase-memory-mcp) is_codebase_memory_mcp_installed ;;
        direnv)              is_direnv_installed ;;
        *)                   return 1 ;;
    esac
}

# -----------------------------------------------------------------------------
# Tool installers
# -----------------------------------------------------------------------------

install_rtk() {
    info "Installing rtk (Rust Token Killer)..."

    if is_rtk_installed; then
        success "rtk is already installed"
        return 0
    fi

    # Check for conflicting rtk installation
    if command_exists rtk; then
        warn "A different 'rtk' command exists (possibly Rust Type Kit)"
        warn "You may need to uninstall it first or adjust your PATH"
        if ! confirm "Continue anyway?"; then
            return 1
        fi
    fi

    # Install from official source
    info "Downloading rtk installer..."
    if curl -fsSL https://raw.githubusercontent.com/bry/rtk/main/install.sh | sh; then
        success "rtk installed successfully"
        info "Verify with: rtk --version && rtk gain"
    else
        error "Failed to install rtk"
        return 1
    fi
}

install_codegraph() {
    info "Installing codegraph..."

    if is_codegraph_installed; then
        success "codegraph is already installed"
        return 0
    fi

    info "Downloading codegraph installer..."
    if curl -fsSL https://raw.githubusercontent.com/colbymchenry/codegraph/main/install.sh | sh; then
        success "codegraph installed successfully"
        info "Verify with: codegraph --version"
    else
        error "Failed to install codegraph"
        return 1
    fi
}

install_uvx() {
    info "Installing uv (provides uvx)..."

    if is_uvx_installed; then
        success "uvx/uv is already installed"
        return 0
    fi

    case "$OS" in
        macos)
            if command_exists brew; then
                info "Installing via Homebrew..."
                if brew install uv; then
                    success "uv installed successfully via Homebrew"
                else
                    error "Failed to install uv via Homebrew"
                    return 1
                fi
            elif command_exists pip3; then
                info "Installing via pip3..."
                if pip3 install uv; then
                    success "uv installed successfully via pip3"
                else
                    error "Failed to install uv via pip3"
                    return 1
                fi
            else
                error "Neither brew nor pip3 found. Please install Homebrew or Python first."
                return 1
            fi
            ;;
        linux)
            if command_exists pip3; then
                info "Installing via pip3..."
                if pip3 install uv; then
                    success "uv installed successfully via pip3"
                else
                    error "Failed to install uv via pip3"
                    return 1
                fi
            elif command_exists pip; then
                info "Installing via pip..."
                if pip install uv; then
                    success "uv installed successfully via pip"
                else
                    error "Failed to install uv via pip"
                    return 1
                fi
            else
                error "pip not found. Please install Python first."
                return 1
            fi
            ;;
        *)
            error "Unsupported OS: $OS"
            error "Please install uv manually: https://docs.astral.sh/uv/"
            return 1
            ;;
    esac

    info "Verify with: uvx --version"
}

install_codebase_memory_mcp() {
    info "Installing codebase-memory-mcp..."

    if is_codebase_memory_mcp_installed; then
        success "codebase-memory-mcp is already installed"
        return 0
    fi

    if ! command_exists npm; then
        error "npm not found. Please install Node.js first."
        error "Visit: https://nodejs.org/"
        return 1
    fi

    info "Installing via npm..."
    if npm install -g codebase-memory-mcp; then
        success "codebase-memory-mcp installed successfully"
    else
        error "Failed to install codebase-memory-mcp"
        error "You may need to run with sudo: sudo npm install -g codebase-memory-mcp"
        return 1
    fi
}

install_direnv() {
    info "Installing direnv..."

    if is_direnv_installed; then
        success "direnv is already installed"
        return 0
    fi

    if command_exists mise; then
        info "mise detected — installing via mise (see mise.toml)..."
        if mise use -g direnv@latest; then
            success "direnv installed successfully via mise"
            return 0
        else
            warn "mise install failed, falling back to OS package manager..."
        fi
    fi

    case "$OS" in
        macos)
            if command_exists brew; then
                info "Installing via Homebrew..."
                if brew install direnv; then
                    success "direnv installed successfully"
                else
                    error "Failed to install direnv via Homebrew"
                    return 1
                fi
            else
                error "Homebrew not found. Please install Homebrew first."
                error "Visit: https://brew.sh/"
                return 1
            fi
            ;;
        linux)
            # Try package managers in order of preference
            if command_exists apt-get; then
                info "Installing via apt..."
                if sudo apt-get update && sudo apt-get install -y direnv; then
                    success "direnv installed successfully"
                else
                    error "Failed to install direnv via apt"
                    return 1
                fi
            elif command_exists dnf; then
                info "Installing via dnf..."
                if sudo dnf install -y direnv; then
                    success "direnv installed successfully"
                else
                    error "Failed to install direnv via dnf"
                    return 1
                fi
            elif command_exists yum; then
                info "Installing via yum..."
                if sudo yum install -y direnv; then
                    success "direnv installed successfully"
                else
                    error "Failed to install direnv via yum"
                    return 1
                fi
            elif command_exists pacman; then
                info "Installing via pacman..."
                if sudo pacman -S --noconfirm direnv; then
                    success "direnv installed successfully"
                else
                    error "Failed to install direnv via pacman"
                    return 1
                fi
            else
                # Fallback to binary download
                info "No package manager detected, downloading binary..."
                local url="https://github.com/direnv/direnv/releases/latest/download/direnv.linux-${ARCH}"
                if curl -sfLo /tmp/direnv "$url" && chmod +x /tmp/direnv; then
                    if [[ -w "/usr/local/bin" ]]; then
                        mv /tmp/direnv /usr/local/bin/direnv
                    else
                        sudo mv /tmp/direnv /usr/local/bin/direnv
                    fi
                    success "direnv installed successfully"
                else
                    error "Failed to download direnv binary"
                    return 1
                fi
            fi
            ;;
        *)
            error "Unsupported OS: $OS"
            error "Please install direnv manually: https://direnv.net/docs/installation.html"
            return 1
            ;;
    esac

    # Provide shell hook instructions
    echo ""
    info "To complete direnv setup, add the hook to your shell config:"
    echo ""
    local shell_name
    shell_name=$(basename "$SHELL")
    case "$shell_name" in
        bash)
            echo '  # Add to ~/.bashrc or ~/.bash_profile:'
            echo '  eval "$(direnv hook bash)"'
            ;;
        zsh)
            echo '  # Add to ~/.zshrc:'
            echo '  eval "$(direnv hook zsh)"'
            ;;
        fish)
            echo '  # Add to ~/.config/fish/config.fish:'
            echo '  direnv hook fish | source'
            ;;
        *)
            echo "  See: https://direnv.net/docs/hook.html"
            ;;
    esac
    echo ""
}

# Install a tool by name
install_tool() {
    local tool="$1"
    case "$tool" in
        rtk)                 install_rtk ;;
        codegraph)           install_codegraph ;;
        uvx)                 install_uvx ;;
        codebase-memory-mcp) install_codebase_memory_mcp ;;
        direnv)              install_direnv ;;
        *)
            error "Unknown tool: $tool"
            return 2
            ;;
    esac
}

# -----------------------------------------------------------------------------
# Main commands
# -----------------------------------------------------------------------------

show_help() {
    cat <<EOF
Claude Code Prerequisites Installer

Usage:
  $(basename "$0") [options] [tool...]

Options:
  --all       Install all prerequisite tools
  --missing   Install only tools that are not currently installed
  --list      List all available tools and their status
  -h, --help  Show this help message

Tools:
  rtk                   Token-optimized CLI proxy (Rust Token Killer)
  codegraph             Codebase knowledge graph indexer
  uvx                   Universal Python package runner (via uv)
  codebase-memory-mcp   MCP server for codebase memory
  direnv                Per-directory environment variable loader

Examples:
  $(basename "$0")               # Interactive mode - choose tools to install
  $(basename "$0") rtk codegraph # Install specific tools
  $(basename "$0") --missing     # Install all missing tools
  $(basename "$0") --all         # Install all tools

Platform: $OS ($ARCH)
EOF
}

list_tools() {
    echo "Available tools:"
    echo ""
    printf "  %-22s %s\n" "TOOL" "STATUS"
    printf "  %-22s %s\n" "----" "------"
    for tool in "${AVAILABLE_TOOLS[@]}"; do
        if is_tool_installed "$tool"; then
            printf "  %-22s \033[32minstalled\033[0m\n" "$tool"
        else
            printf "  %-22s \033[31mnot installed\033[0m\n" "$tool"
        fi
    done
    echo ""
}

install_all() {
    info "Installing all prerequisite tools..."
    echo ""

    local failed=()
    for tool in "${AVAILABLE_TOOLS[@]}"; do
        if ! install_tool "$tool"; then
            failed+=("$tool")
        fi
        echo ""
    done

    if [[ ${#failed[@]} -gt 0 ]]; then
        error "Failed to install: ${failed[*]}"
        return 1
    fi

    success "All tools installed successfully!"
}

install_missing() {
    info "Checking for missing tools..."
    echo ""

    local missing=()
    for tool in "${AVAILABLE_TOOLS[@]}"; do
        if ! is_tool_installed "$tool"; then
            missing+=("$tool")
        fi
    done

    if [[ ${#missing[@]} -eq 0 ]]; then
        success "All tools are already installed!"
        return 0
    fi

    info "Missing tools: ${missing[*]}"
    echo ""

    local failed=()
    for tool in "${missing[@]}"; do
        if ! install_tool "$tool"; then
            failed+=("$tool")
        fi
        echo ""
    done

    if [[ ${#failed[@]} -gt 0 ]]; then
        error "Failed to install: ${failed[*]}"
        return 1
    fi

    success "All missing tools installed successfully!"
}

interactive_mode() {
    echo "Claude Code Prerequisites Installer"
    echo "===================================="
    echo ""
    echo "Platform: $OS ($ARCH)"
    echo ""

    list_tools

    echo "Options:"
    echo "  1) Install all tools"
    echo "  2) Install missing tools only"
    echo "  3) Select specific tools"
    echo "  4) Exit"
    echo ""

    read -r -p "Choose an option [1-4]: " choice

    case "$choice" in
        1)
            echo ""
            install_all
            ;;
        2)
            echo ""
            install_missing
            ;;
        3)
            echo ""
            echo "Available tools:"
            local i=1
            for tool in "${AVAILABLE_TOOLS[@]}"; do
                local status=""
                if is_tool_installed "$tool"; then
                    status=" (installed)"
                fi
                echo "  $i) $tool$status"
                ((i++))
            done
            echo ""
            read -r -p "Enter tool numbers (space-separated, e.g., '1 3 5'): " selections

            local to_install=()
            for num in $selections; do
                local idx=$((num - 1))
                if [[ $idx -ge 0 && $idx -lt ${#AVAILABLE_TOOLS[@]} ]]; then
                    to_install+=("${AVAILABLE_TOOLS[$idx]}")
                fi
            done

            if [[ ${#to_install[@]} -eq 0 ]]; then
                warn "No valid tools selected"
                return 1
            fi

            echo ""
            info "Installing: ${to_install[*]}"
            echo ""

            local failed=()
            for tool in "${to_install[@]}"; do
                if ! install_tool "$tool"; then
                    failed+=("$tool")
                fi
                echo ""
            done

            if [[ ${#failed[@]} -gt 0 ]]; then
                error "Failed to install: ${failed[*]}"
                return 1
            fi

            success "Selected tools installed successfully!"
            ;;
        4)
            info "Exiting..."
            exit 0
            ;;
        *)
            error "Invalid option: $choice"
            return 1
            ;;
    esac
}

# -----------------------------------------------------------------------------
# Main execution
# -----------------------------------------------------------------------------

main() {
    # Handle no arguments - interactive mode
    if [[ $# -eq 0 ]]; then
        interactive_mode
        exit $?
    fi

    # Parse arguments
    local tools_to_install=()

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -h|--help)
                show_help
                exit 0
                ;;
            --list)
                list_tools
                exit 0
                ;;
            --all)
                install_all
                exit $?
                ;;
            --missing)
                install_missing
                exit $?
                ;;
            -*)
                error "Unknown option: $1"
                echo "Use --help for usage information"
                exit 2
                ;;
            *)
                # Treat as tool name
                tools_to_install+=("$1")
                shift
                ;;
        esac
    done

    # Install specified tools
    if [[ ${#tools_to_install[@]} -gt 0 ]]; then
        local failed=()
        for tool in "${tools_to_install[@]}"; do
            # Validate tool name
            local valid=false
            for available in "${AVAILABLE_TOOLS[@]}"; do
                if [[ "$tool" == "$available" ]]; then
                    valid=true
                    break
                fi
            done

            if [[ "$valid" == "false" ]]; then
                error "Unknown tool: $tool"
                error "Available tools: ${AVAILABLE_TOOLS[*]}"
                exit 2
            fi

            if ! install_tool "$tool"; then
                failed+=("$tool")
            fi
            echo ""
        done

        if [[ ${#failed[@]} -gt 0 ]]; then
            error "Failed to install: ${failed[*]}"
            exit 1
        fi

        success "Installation complete!"
    fi
}

main "$@"
