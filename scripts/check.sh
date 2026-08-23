#!/usr/bin/env bash
#
# check.sh - Environment diagnosis for Claude Code configuration
#
# Usage:
#   ./scripts/check.sh        # Human-readable output
#   ./scripts/check.sh --json # JSON output for AI parsing
#
# Exit codes:
#   0 - All checks pass
#   1 - Issues found (some checks failed)
#   2 - Script error
#

set -euo pipefail

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

JSON_OUTPUT=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --json)
            JSON_OUTPUT=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [--json]"
            echo ""
            echo "Options:"
            echo "  --json    Output results in JSON format for AI parsing"
            echo "  -h,--help Show this help message"
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            echo "Use --help for usage information" >&2
            exit 2
            ;;
    esac
done

# -----------------------------------------------------------------------------
# Global state for JSON output
# -----------------------------------------------------------------------------

declare -A PREREQ_RESULTS
declare -A CONFIG_RESULTS
declare -A ENVVAR_RESULTS
ISSUES=()

# -----------------------------------------------------------------------------
# Utility functions
# -----------------------------------------------------------------------------

# Print message in human mode only
human_print() {
    if [[ "$JSON_OUTPUT" == "false" ]]; then
        echo "$@"
    fi
}

# Print colored status
print_status() {
    local status="$1"
    local message="$2"

    if [[ "$JSON_OUTPUT" == "false" ]]; then
        case "$status" in
            ok)     echo -e "  ✅ $message" ;;
            warn)   echo -e "  ⚠️  $message" ;;
            fail)   echo -e "  ❌ $message" ;;
            info)   echo -e "  ℹ️  $message" ;;
        esac
    fi
}

# Add an issue to the issues list
add_issue() {
    ISSUES+=("$1")
}

# -----------------------------------------------------------------------------
# Prerequisite checking functions
# -----------------------------------------------------------------------------

check_rtk() {
    local installed=false
    local version=""
    local install_cmd="curl -fsSL https://raw.githubusercontent.com/bry/rtk/main/install.sh | sh"

    # Check for rtk and specifically rtk gain to distinguish from Rust Type Kit
    if command -v rtk &>/dev/null; then
        if rtk gain --help &>/dev/null 2>&1 || rtk gain &>/dev/null 2>&1; then
            installed=true
            version=$(rtk --version 2>/dev/null | head -1 || echo "unknown")
        fi
    fi

    if [[ "$installed" == "true" ]]; then
        print_status "ok" "rtk: installed ($version)"
        PREREQ_RESULTS["rtk"]='{"installed": true, "version": "'"$version"'"}'
    else
        print_status "fail" "rtk: not installed (Token Killer - not Rust Type Kit)"
        print_status "info" "  Install: $install_cmd"
        PREREQ_RESULTS["rtk"]='{"installed": false, "install_cmd": "'"$install_cmd"'"}'
        add_issue "prerequisite:rtk:not_installed"
    fi
}

check_codegraph() {
    local installed=false
    local version=""
    local install_cmd="curl -fsSL https://raw.githubusercontent.com/colbymchenry/codegraph/main/install.sh | sh"

    if command -v codegraph &>/dev/null; then
        installed=true
        version=$(codegraph --version 2>/dev/null | head -1 || echo "unknown")
    fi

    if [[ "$installed" == "true" ]]; then
        print_status "ok" "codegraph: installed ($version)"
        PREREQ_RESULTS["codegraph"]='{"installed": true, "version": "'"$version"'"}'
    else
        print_status "fail" "codegraph: not installed"
        print_status "info" "  Install: $install_cmd"
        PREREQ_RESULTS["codegraph"]='{"installed": false, "install_cmd": "'"$install_cmd"'"}'
        add_issue "prerequisite:codegraph:not_installed"
    fi
}

check_uvx() {
    local installed=false
    local version=""
    local install_cmd="pip install uv  # or: brew install uv"

    if command -v uvx &>/dev/null; then
        installed=true
        version=$(uvx --version 2>/dev/null | head -1 || echo "unknown")
    elif command -v uv &>/dev/null; then
        # uv provides uvx
        installed=true
        version=$(uv --version 2>/dev/null | head -1 || echo "unknown")
    fi

    if [[ "$installed" == "true" ]]; then
        print_status "ok" "uvx: installed ($version)"
        PREREQ_RESULTS["uvx"]='{"installed": true, "version": "'"$version"'"}'
    else
        print_status "fail" "uvx: not installed"
        print_status "info" "  Install: $install_cmd"
        PREREQ_RESULTS["uvx"]='{"installed": false, "install_cmd": "'"$install_cmd"'"}'
        add_issue "prerequisite:uvx:not_installed"
    fi
}

check_codebase_memory_mcp() {
    local installed=false
    local version=""
    local install_cmd="npm i -g codebase-memory-mcp"

    if command -v codebase-memory-mcp &>/dev/null; then
        installed=true
        version=$(codebase-memory-mcp --version 2>/dev/null | head -1 || echo "unknown")
    elif npm list -g codebase-memory-mcp &>/dev/null 2>&1; then
        installed=true
        version=$(npm list -g codebase-memory-mcp 2>/dev/null | grep codebase-memory-mcp | sed 's/.*@//' || echo "unknown")
    fi

    if [[ "$installed" == "true" ]]; then
        print_status "ok" "codebase-memory-mcp: installed ($version)"
        PREREQ_RESULTS["codebase_memory_mcp"]='{"installed": true, "version": "'"$version"'"}'
    else
        print_status "fail" "codebase-memory-mcp: not installed"
        print_status "info" "  Install: $install_cmd"
        PREREQ_RESULTS["codebase_memory_mcp"]='{"installed": false, "install_cmd": "'"$install_cmd"'"}'
        add_issue "prerequisite:codebase_memory_mcp:not_installed"
    fi
}

check_direnv() {
    local installed=false
    local version=""
    local hooked=false
    local install_cmd="brew install direnv  # or: apt install direnv"

    if command -v direnv &>/dev/null; then
        installed=true
        version=$(direnv --version 2>/dev/null || echo "unknown")
    fi

    # Check if direnv is hooked into shell
    if [[ "$installed" == "true" ]]; then
        # Check common shell rc files for direnv hook
        local shell_name
        shell_name=$(basename "$SHELL")

        case "$shell_name" in
            bash)
                if grep -q "direnv hook bash" ~/.bashrc 2>/dev/null || \
                   grep -q "direnv hook bash" ~/.bash_profile 2>/dev/null; then
                    hooked=true
                fi
                ;;
            zsh)
                if grep -q "direnv hook zsh" ~/.zshrc 2>/dev/null; then
                    hooked=true
                fi
                ;;
            fish)
                if grep -q "direnv hook fish" ~/.config/fish/config.fish 2>/dev/null; then
                    hooked=true
                fi
                ;;
        esac

        # Also check if DIRENV_DIR is set (indicates hook is active in current shell)
        if [[ -n "${DIRENV_DIR:-}" ]]; then
            hooked=true
        fi
    fi

    if [[ "$installed" == "true" ]]; then
        if [[ "$hooked" == "true" ]]; then
            print_status "ok" "direnv: installed ($version), shell hook active"
            PREREQ_RESULTS["direnv"]='{"installed": true, "version": "'"$version"'", "hooked": true}'
        else
            print_status "warn" "direnv: installed ($version), but shell hook not detected"
            print_status "info" "  Add to your shell rc: eval \"\$(direnv hook $shell_name)\""
            PREREQ_RESULTS["direnv"]='{"installed": true, "version": "'"$version"'", "hooked": false}'
            add_issue "prerequisite:direnv:not_hooked"
        fi
    else
        print_status "fail" "direnv: not installed"
        print_status "info" "  Install: $install_cmd"
        PREREQ_RESULTS["direnv"]='{"installed": false, "install_cmd": "'"$install_cmd"'"}'
        add_issue "prerequisite:direnv:not_installed"
    fi
}

# -----------------------------------------------------------------------------
# Config file checking functions
# -----------------------------------------------------------------------------

check_settings_json() {
    local found=false
    local path=""

    # Check standard locations
    if [[ -f "$HOME/.claude/settings.json" ]]; then
        found=true
        path="$HOME/.claude/settings.json"
    elif [[ -f "$HOME/.config/claude/settings.json" ]]; then
        found=true
        path="$HOME/.config/claude/settings.json"
    fi

    if [[ "$found" == "true" ]]; then
        # Validate it's valid JSON
        if jq empty "$path" 2>/dev/null; then
            print_status "ok" "settings.json: found at $path"
            CONFIG_RESULTS["settings_json"]='{"found": true, "path": "'"$path"'", "valid": true}'
        else
            print_status "warn" "settings.json: found at $path but invalid JSON"
            CONFIG_RESULTS["settings_json"]='{"found": true, "path": "'"$path"'", "valid": false}'
            add_issue "config:settings_json:invalid_json"
        fi
    else
        print_status "fail" "settings.json: not found"
        print_status "info" "  Expected at: ~/.claude/settings.json"
        CONFIG_RESULTS["settings_json"]='{"found": false, "expected_path": "~/.claude/settings.json"}'
        add_issue "config:settings_json:not_found"
    fi
}

check_mcp_json() {
    local found=false
    local path=""

    # Check current directory first, then home
    if [[ -f "./.mcp.json" ]]; then
        found=true
        path="./.mcp.json"
    elif [[ -f "$HOME/.mcp.json" ]]; then
        found=true
        path="$HOME/.mcp.json"
    elif [[ -f "$HOME/.claude/.mcp.json" ]]; then
        found=true
        path="$HOME/.claude/.mcp.json"
    fi

    if [[ "$found" == "true" ]]; then
        # Validate it's valid JSON
        if jq empty "$path" 2>/dev/null; then
            print_status "ok" ".mcp.json: found at $path"
            CONFIG_RESULTS["mcp_json"]='{"found": true, "path": "'"$path"'", "valid": true}'
        else
            print_status "warn" ".mcp.json: found at $path but invalid JSON"
            CONFIG_RESULTS["mcp_json"]='{"found": true, "path": "'"$path"'", "valid": false}'
            add_issue "config:mcp_json:invalid_json"
        fi
    else
        print_status "info" ".mcp.json: not found (optional - project-specific)"
        CONFIG_RESULTS["mcp_json"]='{"found": false, "required": false}'
    fi
}

check_profile_resolver() {
    local repo_root
    repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
    local bin_ok=true
    for tool in coding-agent-profile coding-agent-profile-explain ca-claude ca-codex; do
        if ! command -v "$tool" &>/dev/null; then
            bin_ok=false
        fi
    done

    local profile_dir="${CODING_AGENT_PROFILE_DIR:-$HOME/.coding-agent-profiles}"
    local profile_dir_exists=false
    [[ -d "$profile_dir" ]] && profile_dir_exists=true

    if [[ "$bin_ok" == "true" ]]; then
        print_status "ok" "profile resolver: ca-claude/ca-codex/coding-agent-profile on PATH"
    else
        print_status "info" "profile resolver: not on PATH (run scripts/profile-install.sh if you use directory-scoped profiles)"
    fi

    if [[ "$profile_dir_exists" == "true" ]]; then
        local codex_home="${CODEX_HOME:-$HOME/.codex}"
        local stale=0 checked=0
        for dir in "$profile_dir"/*/; do
            [[ -d "$dir" ]] || continue
            local name
            name="$(basename "$dir")"
            [[ -f "$dir/codex/config.toml" ]] || continue
            checked=$((checked + 1))
            local link="$codex_home/$name.config.toml"
            if [[ -L "$link" ]]; then
                local target
                target="$(readlink "$link")"
                [[ "$target" == "$dir"config.toml || "$target" == "${dir%/}/codex/config.toml" ]] || stale=$((stale + 1))
            else
                stale=$((stale + 1))
            fi
        done
        if [[ "$stale" -gt 0 ]]; then
            print_status "warn" "profile resolver: $stale of $checked Codex profile symlink(s) missing/stale — rerun scripts/profile-install.sh"
            add_issue "profile:codex_symlink:stale"
        else
            print_status "ok" "profile resolver: $checked Codex profile symlink(s) healthy"
        fi
        CONFIG_RESULTS["profile_resolver"]='{"bin_on_path": '"$bin_ok"', "profile_dir_exists": true, "codex_symlinks_checked": '"$checked"', "codex_symlinks_stale": '"$stale"'}'
    else
        print_status "info" "profile resolver: $profile_dir does not exist (no profiles configured — see docs/PROFILES.md)"
        CONFIG_RESULTS["profile_resolver"]='{"bin_on_path": '"$bin_ok"', "profile_dir_exists": false}'
    fi
}

check_settings_precedence() {
    # The directory-profile system (docs/PROFILES.md) relies on the observed
    # Claude Code settings cascade (userSettings < projectSettings <
    # localSettings < flagSettings < policySettings) staying true. This is
    # NOT a published contract — a CLI upgrade could silently change it,
    # which would break the safety property that managed/policy settings
    # always win over a profile overlay. Best-effort string check against
    # the installed binary; absence is a loud warning, not a hard failure.
    local claude_bin
    claude_bin="$(command -v claude 2>/dev/null || true)"
    if [[ -z "$claude_bin" ]]; then
        print_status "info" "settings precedence: claude not on PATH, skipping"
        CONFIG_RESULTS["settings_precedence"]='{"checked": false, "reason": "claude not on PATH"}'
        return
    fi
    # Subshell with pipefail off: `grep -q` closes the pipe as soon as it
    # matches, which SIGPIPEs `strings` — under the script's global
    # `set -o pipefail` that non-zero SIGPIPE exit status wins even though
    # grep itself matched, silently flipping a true positive to a false
    # negative. Isolate that here rather than disabling pipefail globally.
    if (set +o pipefail; strings -a "$claude_bin" 2>/dev/null | grep -qi "policySettings"); then
        print_status "ok" "settings precedence: installed claude binary still references policySettings (managed-config layer present)"
        CONFIG_RESULTS["settings_precedence"]='{"checked": true, "policy_layer_present": true}'
    else
        print_status "warn" "settings precedence: installed claude binary has no 'policySettings' string — the assumed managed-config precedence used by docs/PROFILES.md could not be confirmed. Re-verify before trusting profile overlays not to bypass managed policy."
        CONFIG_RESULTS["settings_precedence"]='{"checked": true, "policy_layer_present": false}'
        add_issue "profile:settings_precedence:unconfirmed"
    fi
}

# -----------------------------------------------------------------------------
# Environment variable checking functions
# -----------------------------------------------------------------------------

check_env_var() {
    local var_name="$1"
    local required="${2:-false}"
    local validation="${3:-}"  # Optional validation type: email, url, token

    local is_set=false
    local looks_valid=true
    local hint=""
    local value="${!var_name:-}"

    if [[ -n "$value" ]]; then
        is_set=true

        # Validate format if specified
        case "$validation" in
            email)
                if [[ ! "$value" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]; then
                    looks_valid=false
                    hint="should be an email address"
                fi
                ;;
            url)
                if [[ ! "$value" =~ ^https?:// ]]; then
                    looks_valid=false
                    hint="should be a URL starting with http:// or https://"
                fi
                ;;
            token)
                if [[ ${#value} -lt 10 ]]; then
                    looks_valid=false
                    hint="token appears too short"
                fi
                ;;
        esac
    fi

    # Build JSON result
    local json_result='{"set": '$is_set
    if [[ "$is_set" == "true" && -n "$validation" ]]; then
        json_result+=', "looks_valid": '$looks_valid
        if [[ "$looks_valid" == "false" && -n "$hint" ]]; then
            json_result+=', "hint": "'"$hint"'"'
        fi
    fi
    json_result+='}'

    ENVVAR_RESULTS["$var_name"]="$json_result"

    # Human output
    if [[ "$is_set" == "true" ]]; then
        if [[ "$looks_valid" == "true" ]]; then
            print_status "ok" "$var_name: set"
        else
            print_status "warn" "$var_name: set but $hint"
            add_issue "env_var:$var_name:invalid_format"
        fi
    else
        if [[ "$required" == "true" ]]; then
            print_status "fail" "$var_name: not set (required)"
            add_issue "env_var:$var_name:not_set"
        else
            print_status "info" "$var_name: not set (optional)"
        fi
    fi
}

# -----------------------------------------------------------------------------
# JSON output generation
# -----------------------------------------------------------------------------

generate_json_output() {
    local issues_count=${#ISSUES[@]}
    local all_ok="true"

    if [[ $issues_count -gt 0 ]]; then
        all_ok="false"
    fi

    # Build prerequisites JSON
    local prereq_json="{"
    local first=true
    for key in "${!PREREQ_RESULTS[@]}"; do
        if [[ "$first" == "true" ]]; then
            first=false
        else
            prereq_json+=", "
        fi
        prereq_json+="\"$key\": ${PREREQ_RESULTS[$key]}"
    done
    prereq_json+="}"

    # Build config JSON
    local config_json="{"
    first=true
    for key in "${!CONFIG_RESULTS[@]}"; do
        if [[ "$first" == "true" ]]; then
            first=false
        else
            config_json+=", "
        fi
        config_json+="\"$key\": ${CONFIG_RESULTS[$key]}"
    done
    config_json+="}"

    # Build env_vars JSON
    local envvar_json="{"
    first=true
    for key in "${!ENVVAR_RESULTS[@]}"; do
        if [[ "$first" == "true" ]]; then
            first=false
        else
            envvar_json+=", "
        fi
        envvar_json+="\"$key\": ${ENVVAR_RESULTS[$key]}"
    done
    envvar_json+="}"

    # Build issues array JSON
    local issues_json="["
    first=true
    for issue in "${ISSUES[@]}"; do
        if [[ "$first" == "true" ]]; then
            first=false
        else
            issues_json+=", "
        fi
        issues_json+="\"$issue\""
    done
    issues_json+="]"

    # Output complete JSON
    cat <<EOF
{
  "prerequisites": $prereq_json,
  "config": $config_json,
  "env_vars": $envvar_json,
  "summary": {
    "ok": $all_ok,
    "issues_count": $issues_count,
    "issues": $issues_json
  }
}
EOF
}

# -----------------------------------------------------------------------------
# Main execution
# -----------------------------------------------------------------------------

main() {
    human_print "Claude Code Environment Check"
    human_print "=============================="
    human_print ""

    # Check prerequisites
    human_print "Prerequisites:"
    check_rtk
    check_codegraph
    check_uvx
    check_codebase_memory_mcp
    check_direnv
    human_print ""

    # Check config files
    human_print "Configuration Files:"
    check_settings_json
    check_mcp_json
    check_profile_resolver
    check_settings_precedence
    human_print ""

    # Check environment variables
    human_print "Environment Variables:"
    check_env_var "GITHUB_PERSONAL_ACCESS_TOKEN" "true" "token"
    check_env_var "JIRA_URL" "false" "url"
    check_env_var "JIRA_USERNAME" "false" "email"
    check_env_var "JIRA_API_TOKEN" "false" "token"
    check_env_var "CLICKUP_API_TOKEN" "false" "token"
    check_env_var "SLACK_BOT_TOKEN" "false" "token"
    check_env_var "ANTHROPIC_API_KEY" "false" "token"
    human_print ""

    # Generate output
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        generate_json_output
    else
        # Human summary
        local issues_count=${#ISSUES[@]}
        if [[ $issues_count -eq 0 ]]; then
            echo "Summary: All checks passed! ✅"
            exit 0
        else
            echo "Summary: $issues_count issue(s) found"
            echo ""
            echo "Issues:"
            for issue in "${ISSUES[@]}"; do
                echo "  - $issue"
            done
            exit 1
        fi
    fi

    # Exit code based on issues
    if [[ ${#ISSUES[@]} -gt 0 ]]; then
        exit 1
    fi
    exit 0
}

main "$@"
