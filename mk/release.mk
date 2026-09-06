.PHONY: mutation _mutation-impl mutation-linux _mutation-linux-impl mutate-file release-gate _release-gate-impl _release-gate-quality _release-gate-package _release-gate-smoke _release-gate-mutation _release-gate-mutation-run

mutation: $(VENV_DIR)/.deps.stamp ## Run mutation tests with mutmut
	$(call timed_make,"mutation: total",_mutation-impl)

_mutation-impl:
	@if [[ -n "$(strip $(MUTATION_PATHS_TO_MUTATE))" || -n "$(strip $(MUTATION_TESTS_TO_RUN))" ]]; then \
		$(call log_error,"native mutation cannot honor scoped paths and tests; use mutation-linux"); \
		exit 2; \
	fi
	$(call log_step,"Mutation tests")
	@mkdir -p "$(QUALITY_DIR)"
	@rm -rf "$(ROOT)/.mutmut-cache" "$(ROOT)/mutants" "$(MUTATION_STATS_JSON)"
	@if [[ -n "$(MUTATION_ARGS)" ]]; then \
		printf "mutation selectors: $(words $(MUTATION_ARGS)); progress log: %s\n" "$(MUTATION_RUN_LOG)"; \
		set +e; \
		$(MUTATION_QOS_PREFIX) $(MUTMUT) run --max-children $(MUTATION_MAX_CHILDREN) $(MUTATION_ARGS) > "$(MUTATION_RUN_LOG)" 2>&1; \
		status=$$?; \
		set -e; \
		if [[ $$status -ne 0 ]]; then tail -n 80 "$(MUTATION_RUN_LOG)"; exit $$status; fi; \
	else \
		$(MUTATION_QOS_PREFIX) $(MUTMUT) run --max-children $(MUTATION_MAX_CHILDREN); \
	fi
	@if [[ -z "$(MUTATION_ARGS)" ]]; then $(MUTMUT) results; fi
	@$(PYTHON) "$(ROOT)/scripts/check_mutation_stats.py" \
		--mutants-dir "$(ROOT)/mutants" \
		--output-json "$(MUTATION_STATS_JSON)" \
		--min-score "$(MUTATION_MIN_SCORE)" \
		$(if $(MUTATION_ARGS),--include-mutants $(MUTATION_ARGS),) \
		--fail-statuses $(MUTATION_FAIL_STATUSES)
	$(call log_success,"Mutation tests passed")

mutation-linux: ## Run mutation tests inside a Linux container
	$(call timed_make,"mutation-linux: total",_mutation-linux-impl)

mutate-file: ## Run one source-and-affected-tests mutation slice
	@test -n "$(MUTATE_FILE)" || { $(call log_error,"MUTATE_FILE is required"); exit 2; }
	@test -n "$(MUTATION_TESTS_TO_RUN)" || { $(call log_error,"MUTATION_TESTS_TO_RUN is required"); exit 2; }
	@test -n "$(MUTATION_ARGS)" || { $(call log_error,"MUTATION_ARGS is required for exact changed-callable scope"); exit 2; }
	$(call timed_make,"mutate-file: $(MUTATE_FILE)",MUTATION_PATHS_TO_MUTATE="$(MUTATE_FILE)" MUTATION_TESTS_TO_RUN="$(MUTATION_TESTS_TO_RUN)" mutation-linux)

_mutation-linux-impl:
	@paths="$(MUTATION_PATHS_TO_MUTATE)"; \
	if [[ -z "$$paths" ]]; then \
		paths="$$(PYTHONPATH="$(PYTHONPATH)" "$(PYTHON)" "$(MUTATION_PATH_SELECTOR)" --root "$(ROOT)" --diff-base "$(MUTATION_DIFF_BASE)" --format make)"; \
	fi; \
	if [[ -z "$$paths" ]]; then \
		$(call log_error,"mutation-linux selected zero changed source files; refusing unscoped mutation"); \
		exit 2; \
	fi; \
	tests="$(MUTATION_TESTS_TO_RUN)"; \
	if [[ -z "$$tests" ]]; then \
		tests="$$(PYTHONPATH="$(PYTHONPATH)" "$(PYTHON)" "$(MUTATION_TEST_SELECTOR)" --root "$(ROOT)" --tier "$(MUTATION_TEST_TIER)" --base "$(MUTATION_DIFF_BASE)" | tr '\n' ' ')"; \
	fi; \
	if [[ -z "$$tests" ]]; then \
		$(call log_error,"mutation-linux selected zero affected tests; refusing unscoped mutation"); \
		exit 2; \
	fi; \
	printf "mutation-linux: resolve changed paths: %s\n" "$$paths"; \
	printf "mutation-linux: resolve affected tests: %s\n" "$$tests"; \
	MCP_BROKER_MUTATION_IMAGE="$(MUTATION_IMAGE)" \
		MCP_BROKER_MUTATION_MAX_CHILDREN="$(MUTATION_MAX_CHILDREN)" \
		MCP_BROKER_MUTATION_ARGS="$(MUTATION_ARGS)" \
		MCP_BROKER_MUTATION_DEBUG="$(MUTATION_DEBUG)" \
		MCP_BROKER_MUTATION_PATHS_TO_MUTATE="$$paths" \
		MCP_BROKER_MUTATION_TESTS_TO_RUN="$$tests" \
		MCP_BROKER_MUTATION_LOG="$(MUTATION_LOG)" \
		MCP_BROKER_MUTATION_MUTANTS_DIR="$(MUTATION_MUTANTS_DIR)" \
		$(MUTATION_QOS_PREFIX) "$(ROOT)/scripts/linux-mutation.sh"

release-gate: ## Run release gates with resource-bounded mutation
	$(call timed_make,"release-gate: total",_release-gate-impl)

_release-gate-impl:
	$(call timed_make,"release-gate: deps",deps)
ifeq ($(RELEASE_GATE_PARALLEL),1)
	$(call timed_make,"release-gate: parallel children",$(call parallel_make_args,$(RELEASE_GATE_JOBS)) _release-gate-quality _release-gate-package _release-gate-smoke _release-gate-mutation)
else
	$(call timed_make,"release-gate: sequential quality-gate",_release-gate-quality)
	$(call timed_make,"release-gate: sequential package-check",_release-gate-package)
	$(call timed_make,"release-gate: sequential release-smoke",_release-gate-smoke)
	$(call timed_make,"release-gate: sequential mutation",_release-gate-mutation)
endif
	$(call log_success,"Release gate passed")

_release-gate-quality:
	@mkdir -p "$(RELEASE_GATE_LOG_DIR)"
	$(call timed_make,"release-gate child: quality-gate",PYTEST_WORKERS="$(PYTEST_RELEASE_WORKERS)" quality-gate,"$(RELEASE_GATE_LOG_DIR)/quality-gate.log")

_release-gate-package:
	@mkdir -p "$(RELEASE_GATE_LOG_DIR)"
	$(call timed_make,"release-gate child: package-check",package-check,"$(RELEASE_GATE_LOG_DIR)/package-check.log")

_release-gate-smoke:
	@mkdir -p "$(RELEASE_GATE_LOG_DIR)"
	$(call timed_make,"release-gate child: release-smoke",release-smoke,"$(RELEASE_GATE_LOG_DIR)/release-smoke.log")

_release-gate-mutation:
	@mkdir -p "$(RELEASE_GATE_LOG_DIR)"
	$(call timed_make,"release-gate child: $(RELEASE_MUTATION_TARGET)",_release-gate-mutation-run,"$(RELEASE_GATE_LOG_DIR)/$(RELEASE_MUTATION_TARGET).log")

_release-gate-mutation-run:
	@paths="$(MUTATION_PATHS_TO_MUTATE)"; \
	if [[ -z "$$paths" ]]; then \
		paths="$$(PYTHONPATH="$(PYTHONPATH)" "$(PYTHON)" "$(MUTATION_PATH_SELECTOR)" --root "$(ROOT)" --diff-base "$(MUTATION_DIFF_BASE)" --format make)"; \
	fi; \
	if [[ -z "$$paths" ]]; then \
		$(call log_error,"release-gate selected zero changed source files; refusing unscoped mutation"); \
		exit 2; \
	fi; \
	tests="$(MUTATION_TESTS_TO_RUN)"; \
	if [[ -z "$$tests" ]]; then \
		tests="$$(PYTHONPATH="$(PYTHONPATH)" "$(PYTHON)" "$(MUTATION_TEST_SELECTOR)" --root "$(ROOT)" --tier "$(MUTATION_TEST_TIER)" --base "$(MUTATION_DIFF_BASE)" | tr '\n' ' ')"; \
	fi; \
	if [[ -z "$$tests" ]]; then \
		$(call log_error,"release-gate selected zero affected tests; refusing unscoped mutation"); \
		exit 2; \
	fi; \
	$(MAKE) --no-print-directory MUTATION_PATHS_TO_MUTATE="$$paths" MUTATION_TESTS_TO_RUN="$$tests" MUTATION_MAX_CHILDREN="$(MUTATION_RELEASE_CHILDREN)" $(RELEASE_MUTATION_TARGET)
