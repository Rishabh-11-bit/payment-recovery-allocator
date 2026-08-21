#!/usr/bin/env bash
# One-time repo bootstrap. Run from inside the unzipped folder.
#
#   chmod +x bootstrap.sh && ./bootstrap.sh
#
# Requires the GitHub CLI (gh) to be installed and authenticated:
#   gh auth login
#
# If you don't have gh, create the repo manually on github.com (public, no README,
# no .gitignore, no license) and then run the commands under MANUAL below.

set -euo pipefail

REPO_NAME="payment-recovery-allocator"
DESCRIPTION="Attempt allocation under a capped retry budget — Razorpay AI Builder Internship 2026, Track 03"

echo "==> Initialising git"
git init -b main
git add .
git commit -m "Project context, constraints, and prior-art boundary

Phase 0. Documentation before code: architecture rules, the running
build log, and the README boundary against Razorpay's existing
Optimizer / Smart Router / Intelligent Retry Engine stack."

echo "==> Creating public repo on GitHub"
gh repo create "$REPO_NAME" \
  --public \
  --source=. \
  --remote=origin \
  --description "$DESCRIPTION" \
  --push

echo
echo "Done. Repo URL:"
gh repo view --json url --jq .url

# ---------------------------------------------------------------------------
# MANUAL (no gh CLI)
#
#   git init -b main
#   git add .
#   git commit -m "Project context, constraints, and prior-art boundary"
#   git remote add origin git@github.com:Rishabh-11-bit/payment-recovery-allocator.git
#   git push -u origin main
#
# Use the https:// remote instead if you aren't set up with SSH keys.
# ---------------------------------------------------------------------------
