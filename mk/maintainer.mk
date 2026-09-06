.PHONY: require-violations-tool require-grade-quality-tool violations grade-quality maintainer-violations maintainer-grade-quality
.PHONY: hooks-install hook-secret-scan hook-pre-commit

hooks-install: ## Install portable Git hooks without replacing existing hooks
	@$(PYTHON) "$(ROOT)/scripts/install_git_hooks.py" --root "$(ROOT)" --hooks-dir "$(GIT_HOOKS_DIR)"

hook-secret-scan: ## Scan staged changes for secrets; require gitleaks
	@command -v "$(GITLEAKS)" >/dev/null || { printf 'Missing required scanner: gitleaks\n' >&2; exit 2; }
	@git -C "$(ROOT)" diff --cached --name-only -z | $(PYTHON) -c 'import sys; count = sys.stdin.buffer.read().count(b"\0"); print(f"Secret scan: {count} staged paths"); sys.exit(0 if count else 2)'
	@"$(GITLEAKS)" git --pre-commit --staged --redact=100 --no-banner --no-color --ignore-gitleaks-allow "$(ROOT)"

hook-pre-commit: hook-secret-scan ## Scan secrets and run commit-tier affected tests
	@PYTHON="$(PYTHON)" "$(ROOT)/.cits/test-impact.sh" --tier commit --repo "$(ROOT)"

require-violations-tool:
	@test -x "$(CHECK_VIOLATIONS)" || { $(call log_error,"Missing maintainer tool: CHECK_VIOLATIONS=$(CHECK_VIOLATIONS)"); exit 2; }

require-grade-quality-tool: require-violations-tool
	@test -x "$(GRADE_QUALITY)" || { $(call log_error,"Missing maintainer tool: GRADE_QUALITY=$(GRADE_QUALITY)"); exit 2; }

violations: maintainer-violations

grade-quality: maintainer-grade-quality

maintainer-violations: require-violations-tool
	@mkdir -p "$(QUALITY_DIR)"
	@for generated_path in $(GENERATED_SCAN_EXCLUDE_PATHS); do rm -rf $$generated_path; done
	@"$(CHECK_VIOLATIONS)" \
		--repo-root "$(ROOT)" \
		--jobs "$(VIOLATIONS_JOBS)" \
		$(VIOLATIONS_FLAGS) \
		--log --log-file "$(VIOLATIONS_LOG)" \
		--json --json-file "$(VIOLATIONS_JSON)"

maintainer-grade-quality: require-grade-quality-tool maintainer-violations
	@mkdir -p "$(QUALITY_DIR)"
	@"$(GRADE_QUALITY)" \
		--no-refresh \
		--violations-json "$(VIOLATIONS_JSON)" \
		--output-json "$(GRADE_REPORT_JSON)" \
		"$(ROOT)"
	@$(PYTHON) "$(ROOT)/scripts/enforce_grade_quality.py" "$(GRADE_REPORT_JSON)"
