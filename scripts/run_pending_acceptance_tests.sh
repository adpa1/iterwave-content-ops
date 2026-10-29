#!/usr/bin/env bash
set -euo pipefail

# Run from repository root. No production DB, platform credentials, or AI keys are needed.
python -m pytest -q \
  tests/test_tenant_registry.py \
  tests/test_tenant_switch_acceptance.py \
  tests/test_account_login.py \
  tests/test_platform_accounts.py \
  tests/test_global_monitor.py \
  tests/test_media_recommendations.py \
  tests/test_scheduler_config_and_dedup.py \
  tests/test_scheduler_jobs.py
