# Phase 0 targets are filled in as each step lands.
.PHONY: check verify
check:
	@bash scripts/check-tools.sh
verify:
	@bash scripts/verify-phase0.sh
