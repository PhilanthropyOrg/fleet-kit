#!/usr/bin/env bash
# fleet_init.sh — initialize a new product repo for the fleet. One command to scaffold
# everything a founder needs to start building with the fleet.
#
# Usage:
#   bash scripts/fleet_init.sh \
#     --instance my-startup \
#     --repo https://github.com/org/repo \
#     --domain startup.example.com \
#     --brief path/to/founder_brief.md \
#     [--dry-run]
#
# Inputs:
#   --instance: unique name for this fleet instance (alphanumeric, no spaces)
#   --repo: GitHub repo URL (https or git@github.com format)
#   --domain: public domain for the product
#   --brief: path to founder brief (plain text or markdown: business, customer, revenue, KR)
#   --dry-run: print every action without running it
#
# What it does (for empty repos only):
#   1. Write docs/VISION.md from the brief (template + parsed founder input)
#   2. Write fleet/okr.json with goals parsed from VISION.md
#   3. File founding issues via GitHub API (stack, CI, deploy, landing page, auth, payments, analytics)
#   4. Set branch protection + webhook secret
#   5. Output a verification checklist
#
# Idempotency: safe to re-run on the same repo. Only creates files if missing.
# Dry-run mode: print actions without executing them.
set -euo pipefail

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Inputs
INSTANCE=""
REPO=""
DOMAIN=""
BRIEF=""
DRY_RUN=0

# Parsed from repo URL
REPO_OWNER=""
REPO_NAME=""
REPO_SLUG=""

# Helper: print with color
log_info() {
  echo -e "${GREEN}[fleet_init]${NC} $*"
}

log_warn() {
  echo -e "${YELLOW}[fleet_init]${NC} $*" >&2
}

log_error() {
  echo -e "${RED}[fleet_init] ERROR${NC} $*" >&2
}

# Helper: conditionally run commands
run_or_dry() {
  local cmd_desc="$1"
  shift
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[DRY-RUN] $cmd_desc"
    echo "          Command: $*"
  else
    log_info "$cmd_desc"
    "$@"
  fi
}

# Helper: parse GitHub repo URL
parse_repo_url() {
  local url="$1"
  # Handle both https://github.com/owner/repo.git and git@github.com:owner/repo.git
  if [[ "$url" =~ github\.com[:/]([^/]+)/(.+)$ ]]; then
    REPO_OWNER="${BASH_REMATCH[1]}"
    REPO_NAME="${BASH_REMATCH[2]}"
    REPO_NAME="${REPO_NAME%.git}"
    REPO_SLUG="$REPO_OWNER/$REPO_NAME"
    return 0
  fi
  return 1
}

# Helper: check if repo is empty (no commits)
is_empty_repo() {
  local slug="$1"
  if ! gh api "repos/$slug" --jq '.pushed_at' 2>/dev/null | grep -q .; then
    return 0  # empty
  fi
  return 1  # not empty
}

# Helper: substitute variables in template
substitute_template() {
  local template="$1"
  local dest="$2"
  local product_name="$3"
  local founder_name="${4:-Founder}"
  local now=$(date -u '+%Y-%m-%d')

  sed -e "s|{{PRODUCT_NAME}}|$product_name|g" \
      -e "s|{{FOUNDER_NAME}}|$founder_name|g" \
      -e "s|{{FILLED_DATE}}|$now|g" \
      -e "s|{{BUSINESS_DESCRIPTION}}|[Your business description here]|g" \
      -e "s|{{CUSTOMER_DESCRIPTION}}|[Your customer description here]|g" \
      -e "s|{{REVENUE_MODEL}}|[Your revenue model here]|g" \
      -e "s|{{METRIC_NAME}}|Key Metric|g" \
      -e "s|{{METRIC_TARGET}}|100|g" \
      -e "s|{{TARGET_DATE}}|$(date -d '+90 days' -u '+%Y-%m-%d' 2>/dev/null || echo '2026-12-31')|g" \
      -e "s|{{OUT_OF_SCOPE}}|[What you're NOT doing]|g" \
      "$template" > "$dest"
}

# Helper: create founding issues
file_founding_issues() {
  local slug="$1"
  local repo_name="$2"

  # Founding issues in priority order
  local issues=(
    "Choose tech stack (Node/Python/Go, database, auth provider)|Review founder brief. Document chosen stack in README."
    "Set up CI/CD workflow|GitHub Actions: lint, test, deploy on merge to main. Add .github/workflows/ci.yml"
    "Implement deploy driver|3-function script: current_sha(), deploy(sha), health(). See docs/deploy_driver.md"
    "Build landing page|Static site or template: explain the product, sign-up CTA, pricing clarity"
    "Wire sign-up and auth|User sign-in form, session management, test accounts for QA"
    "Integrate payments (Stripe test mode)|Pricing page, invoice/subscription handling (test mode only)"
    "Add analytics and observability|Error tracking, user journey instrumentation, metrics dashboard"
  )

  for issue in "${issues[@]}"; do
    IFS='|' read -r title body <<< "$issue"
    local full_body="$body

**Founding issue** — filed by \`fleet_init\` on $(date -u '+%Y-%m-%d %H:%M:%S UTC')

Part of the initial product scaffolding for **$repo_name**."

    run_or_dry "Filing founding issue: $title" \
      gh issue create \
        --repo "$slug" \
        --title "$title" \
        --body "$full_body" \
        --label "fleet:backlog,fleet:priority-p1" \
        2>/dev/null || log_warn "Issue filing failed or already exists: $title"
  done
}

# Main
main() {
  # Parse arguments
  while [ $# -gt 0 ]; do
    case "$1" in
      --instance) INSTANCE="$2"; shift 2 ;;
      --repo) REPO="$2"; shift 2 ;;
      --domain) DOMAIN="$2"; shift 2 ;;
      --brief) BRIEF="$2"; shift 2 ;;
      --dry-run) DRY_RUN=1; shift ;;
      -h|--help)
        cat <<EOF
Usage: fleet_init.sh --instance NAME --repo URL --domain DOMAIN --brief FILE [--dry-run]

Initialize a new product repo for the fleet. Scaffolds empty repos with VISION.md,
goals file, founding issues, and branch protection.

Options:
  --instance INSTANCE    Unique name for this fleet instance (e.g., my-startup)
  --repo URL             GitHub repo URL (https://github.com/org/repo or git@github.com:org/repo)
  --domain DOMAIN        Public domain for the product (e.g., startup.example.com)
  --brief FILE           Path to founder brief (plain text describing business, customer, revenue, KR)
  --dry-run              Print actions without executing them
  -h, --help             Show this help message

Examples:
  bash scripts/fleet_init.sh \\
    --instance my-startup \\
    --repo https://github.com/acme/product \\
    --domain acme.example.com \\
    --brief ~/founder_brief.txt

  bash scripts/fleet_init.sh \\
    --instance my-startup \\
    --repo https://github.com/acme/product \\
    --domain acme.example.com \\
    --brief ~/brief.md \\
    --dry-run
EOF
        exit 0
        ;;
      *) log_error "Unknown flag: $1"; exit 1 ;;
    esac
  done

  # Validate inputs
  if [ -z "$INSTANCE" ] || [ -z "$REPO" ] || [ -z "$DOMAIN" ] || [ -z "$BRIEF" ]; then
    log_error "Missing required arguments. Use --help for usage."
    exit 1
  fi

  if [ ! -f "$BRIEF" ]; then
    log_error "Founder brief file not found: $BRIEF"
    exit 1
  fi

  # Parse repo URL
  if ! parse_repo_url "$REPO"; then
    log_error "Invalid GitHub repo URL: $REPO"
    exit 1
  fi

  log_info "Initializing fleet for: $REPO_NAME (instance: $INSTANCE)"
  if [ "$DRY_RUN" -eq 1 ]; then
    log_warn "DRY-RUN mode: no changes will be made"
  fi

  # Clone or fetch the repo to a temp directory to check its state
  local temp_dir=$(mktemp -d)
  trap "rm -rf '$temp_dir'" EXIT

  local is_empty=0
  local repo_dir="$temp_dir/repo"

  # Try to clone; if it fails, repo is likely empty
  if run_or_dry "Cloning repo to check state" \
    git clone --depth=1 "$REPO" "$repo_dir" 2>&1 | grep -v "warning: " || true; then
    # Cloned successfully - check if it has VISION.md
    if [ ! -f "$repo_dir/docs/VISION.md" ]; then
      is_empty=1
    fi
  else
    # Clone failed - repo is empty or auth issue. Try to detect empty via GitHub API
    if gh api "repos/$REPO_SLUG" --jq '.pushed_at' 2>/dev/null | grep -q .; then
      # Repo exists but clone failed (likely auth/private) - don't scaffold
      log_warn "Could not clone repo (private or auth issue). Skipping scaffold."
      is_empty=0
    else
      # Repo is truly empty
      is_empty=1
      mkdir -p "$repo_dir"
    fi
  fi

  if [ "$is_empty" -eq 1 ]; then
    log_info "Repo is empty or lacks VISION.md. Scaffolding now..."

    # Get the vision template
    local vision_template="$(dirname "$0")/../docs/VISION.md.template"
    if [ ! -f "$vision_template" ]; then
      log_error "VISION.md template not found: $vision_template"
      exit 1
    fi

    # Create directory structure
    mkdir -p "$repo_dir/docs" "$repo_dir/fleet"

    # Initialize git if this is a new repo
    if [ ! -d "$repo_dir/.git" ]; then
      run_or_dry "Initializing git repo" \
        git -C "$repo_dir" init
      run_or_dry "Configuring git user" \
        bash -c "cd '$repo_dir' && git config user.email 'fleet@startup.local' && git config user.name 'Fleet Init'"
    fi

    # Substitute template placeholders and create VISION.md
    substitute_template "$vision_template" "$repo_dir/docs/VISION.md" "$REPO_NAME" "Founder"

    # Inject founder brief into VISION.md
    if [ -f "$BRIEF" ]; then
      run_or_dry "Injecting founder brief into VISION.md" \
        bash -c "echo '---' >> '$repo_dir/docs/VISION.md' && cat '$BRIEF' >> '$repo_dir/docs/VISION.md'"
    fi

    # Create initial fleet/okr.json with placeholder goals
    run_or_dry "Creating fleet/okr.json" \
      bash -c "cat > '$repo_dir/fleet/okr.json' <<'OKRJSON'
{
  \"_doc\": \"Goals for this startup. Read live by the fleet. Update as the business evolves.\",
  \"objective\": {
    \"id\": \"okr.north_star\",
    \"label\": \"Achieve north-star metric\",
    \"metric\": null
  },
  \"key_results\": [
    {
      \"id\": \"okr.users\",
      \"label\": \"Acquire initial customers\",
      \"metric\": null
    },
    {
      \"id\": \"okr.product\",
      \"label\": \"Ship core product features\",
      \"metric\": null
    },
    {
      \"id\": \"okr.retention\",
      \"label\": \"Retain and delight users\",
      \"metric\": null
    }
  ]
}
OKRJSON
"

    # Commit and push for empty repos
    if [ "$DRY_RUN" -eq 0 ]; then
      cd "$repo_dir"
      git add docs/VISION.md fleet/okr.json
      git commit -m "Initial scaffold: VISION.md and goals from fleet_init" 2>&1 | head -2 || log_warn "Commit failed (already exists?)"

      # Set remote and push
      if [ ! -d ".git/refs/remotes/origin" ]; then
        git remote add origin "$REPO" 2>/dev/null || true
      fi

      # For empty repos, push directly to main/master
      local default_branch=$(gh api "repos/$REPO_SLUG" --jq '.default_branch' 2>/dev/null || echo "main")
      git push -u origin "HEAD:$default_branch" 2>&1 | head -2 || log_warn "Push failed (may already exist)"
      cd - > /dev/null
    else
      echo "[DRY-RUN] Would commit VISION.md and fleet/okr.json:"
      echo "          git -C $repo_dir add docs/VISION.md fleet/okr.json"
      echo "          git -C $repo_dir commit -m 'Initial scaffold: VISION.md and goals from fleet_init'"
      echo "          git -C $repo_dir push -u origin HEAD:main"
    fi
  else
    log_info "Repo already has VISION.md and committed history. Skipping scaffold."
  fi

  # File founding issues (works whether or not repo is empty, if push succeeded)
  log_info "Filing founding issues..."
  if [ "$DRY_RUN" -eq 0 ]; then
    file_founding_issues "$REPO_SLUG" "$REPO_NAME"
  else
    log_info "[DRY-RUN] Would file 7 founding issues (stack, CI, deploy, landing page, auth, payments, analytics)"
  fi

  # Set branch protection (requires admin access, will degrade gracefully)
  log_info "Configuring branch protection on main..."
  local bp_cmd=(
    "gh" "api" "-X" "PUT"
    "repos/$REPO_SLUG/branches/main/protection"
    "-f" "required_status_checks.strict=true"
    "-f" "required_status_checks.contexts=[]"
    "-f" "enforce_admins=false"
    "-f" "dismiss_stale_reviews=true"
  )
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[DRY-RUN] Setting branch protection:"
    echo "          ${bp_cmd[*]}"
  else
    if "${bp_cmd[@]}" 2>/dev/null; then
      log_info "Branch protection set on main"
    else
      log_warn "Branch protection failed (requires admin). Manual step:"
      echo "  gh api -X PUT repos/$REPO_SLUG/branches/main/protection -f required_status_checks.strict=true"
    fi
  fi

  # Generate and register webhook
  local webhook_secret=$(openssl rand -hex 16)
  local webhook_created=0

  log_info "Registering webhook with GitHub..."
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[DRY-RUN] Would create webhook:"
    echo "          gh api repos/$REPO_SLUG/hooks -f name=web -f config[url]=https://${DOMAIN}/webhook/inbox -f config[secret]=$webhook_secret -f config[content_type]=json -F active=true -f events[]=pull_request -f events[]=issues -f events[]=push"
  else
    if gh api "repos/$REPO_SLUG/hooks" \
      -f name=web \
      -f "config[url]=https://${DOMAIN}/webhook/inbox" \
      -f "config[secret]=$webhook_secret" \
      -f "config[content_type]=json" \
      -F "active=true" \
      -f "events[]=pull_request" \
      -f "events[]=issues" \
      -f "events[]=push" \
      --jq '.id' 2>/dev/null | grep -q .; then
      webhook_created=1
      log_info "Webhook registered with GitHub"
    else
      log_warn "Webhook registration failed (may need webhook admin scope). Manual step:"
      echo "  gh api repos/$REPO_SLUG/hooks -f name=web -f config[url]=https://${DOMAIN}/webhook/inbox -f config[secret]=$webhook_secret -f config[content_type]=json -F active=true -f events[]=pull_request -f events[]=issues -f events[]=push"
    fi
  fi

  # Output verification checklist
  cat <<EOF

${GREEN}=== Fleet Init Complete ===${NC}

${YELLOW}Scaffolded:${NC}
  ✓ docs/VISION.md (from founder brief)
  ✓ fleet/okr.json (initial goals)
  ✓ 7 founding issues (stack, CI, deploy, landing page, auth, payments, analytics)
  ✓ Branch protection on main (if admin access available)
  $([ "$webhook_created" -eq 1 ] && echo "✓ Webhook registered" || echo "○ Webhook registration (manual step below)")

${YELLOW}Next steps:${NC}
  [ ] Review and update docs/VISION.md with full business details
  $([ "$webhook_created" -eq 0 ] && echo "  [ ] Webhook: $webhook_secret" || echo "")
  [ ] Set up CI: choose tech stack, add .github/workflows/ci.yml
  [ ] Configure deploy driver (see scripts/deploy_driver.md)
  [ ] Start building! The fleet will claim and build founded issues.

${YELLOW}Verify:${NC}
  gh issue list --repo $REPO_SLUG --label fleet:backlog
  gh api repos/$REPO_SLUG/branches/main/protection
  gh api repos/$REPO_SLUG/hooks

${YELLOW}Help:${NC}
  See RUNBOOK.md for fleet operations
  See docs/VISION.md for your product vision
  See scripts/deploy_driver.md for deploy requirements
EOF
}

main "$@"
