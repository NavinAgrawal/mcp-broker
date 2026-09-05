.PHONY: test _test-impl _test-targeted _test-unit-fanout _test-journey-fanout _test-live-fanout _test-e2e-fanout test-unit test-journey test-live test-live-targeted test-e2e test-quick test-cov xdist-benchmark precommit _precommit-impl _precommit-unit-fanout _precommit-journey-fanout quality-gate

test: ## Run all test tiers in parallel
	$(call timed_make,"test: total",_test-impl PYTEST_WORKERS="$(PYTEST_WORKERS)")

_test-impl:
	$(call log_step,"All test tiers")
ifneq ($(strip $(PYTEST_ARGS)),)
	$(call timed_make,"test: targeted tests",_test-targeted)
else
	$(call timed_make,"test: all tiers",$(call parallel_make_args,$(TEST_JOBS)) _test-unit-fanout _test-journey-fanout _test-live-fanout _test-e2e-fanout)
endif
	$(call log_success,"All test tiers passed")

_test-targeted:
	$(call log_step,"Targeted tests")
	@mkdir -p $(COV_DIR) $(TEST_LOG_DIR)
	@PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m pytest $(PYTEST_TARGETED_COMMON) $(PYTEST_ARGS)
	$(call log_success,"Targeted tests passed")

_test-unit-fanout:
	$(call timed_make,"test child: unit",test-unit PYTEST_WORKERS="$(PYTEST_FANOUT_WORKERS)" RUNTIME_ROOT="$(TEST_RUNTIME_ROOT)/unit" LIVE_CONFIG_PATH="$(LIVE_CONFIG_PATH)")

_test-journey-fanout:
	$(call timed_make,"test child: journey",test-journey PYTEST_WORKERS="$(PYTEST_FANOUT_WORKERS)" RUNTIME_ROOT="$(TEST_RUNTIME_ROOT)/journey" LIVE_CONFIG_PATH="$(LIVE_CONFIG_PATH)")

_test-live-fanout:
	$(call timed_make,"test child: live",test-live PYTEST_WORKERS="$(PYTEST_FANOUT_WORKERS)" RUNTIME_ROOT="$(TEST_RUNTIME_ROOT)/live" LIVE_CONFIG_PATH="$(LIVE_CONFIG_PATH)")

_test-e2e-fanout:
	$(call timed_make,"test child: e2e",test-e2e PYTEST_WORKERS="$(PYTEST_FANOUT_WORKERS)" RUNTIME_ROOT="$(TEST_RUNTIME_ROOT)/e2e" LIVE_CONFIG_PATH="$(LIVE_CONFIG_PATH)")

test-unit: ## Run unit tests
	$(call log_step,"Unit tests")
	@mkdir -p $(COV_DIR) $(TEST_LOG_DIR)
	@PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m pytest $(PYTEST_COMMON) $(PYTEST_UNIT_TARGETS)
	$(call log_success,"Unit tests passed")

test-journey: ## Run journey tests
	$(call log_step,"Journey tests")
	@PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m pytest $(PYTEST_COMMON) $(PYTEST_JOURNEY_TARGETS)
	$(call log_success,"Journey tests passed")

test-live: ## Run live tests
	$(call log_step,"Live tests")
	@MCP_BROKER_LIVE_CONFIG_PATH="$(LIVE_CONFIG_PATH)" PYTHONPATH="$(PYTHONPATH)" \
		$(PYTHON) -m pytest $(PYTEST_LIVE_COMMON) $(PYTEST_LIVE_TARGETS)
	$(call log_success,"Live tests passed")

test-live-targeted: ## Run selected live tests with the live timeout budget
	@test -n "$(PYTEST_ARGS)" || { $(call log_error,"PYTEST_ARGS is required"); exit 2; }
	$(call log_step,"Targeted live tests")
	@MCP_BROKER_LIVE_CONFIG_PATH="$(LIVE_CONFIG_PATH)" PYTHONPATH="$(PYTHONPATH)" \
		$(PYTHON) -m pytest $(PYTEST_LIVE_COMMON) $(PYTEST_ARGS)
	$(call log_success,"Targeted live tests passed")

test-e2e: ## Run e2e tests
	$(call log_step,"E2E tests")
	@PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m pytest $(PYTEST_COMMON) $(PYTEST_E2E_TARGETS)
	$(call log_success,"E2E tests passed")

test-quick: ## Fast feedback: unit tests only, fail fast
	$(call log_step,"Quick tests")
	@PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m pytest $(PYTEST_COMMON) -x --no-header -q $(PY_UNIT_DIR)
	$(call log_success,"Quick tests passed")

test-cov: ## Run tests with coverage gate
	$(call log_step,"Coverage tests")
	@mkdir -p $(COV_DIR)
	@rm -f $(COV_FILE)
	@PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m pytest $(PYTEST_COV_COMMON) \
		--cov=$(COV_SRC) --cov-branch --cov-report=term-missing --cov-fail-under=$(COV_FAIL_UNDER) tests
	$(call log_success,"Coverage gate passed")

xdist-benchmark: ## Compare pytest xdist distribution strategies for unit/journey targets
	$(call log_step,"xdist benchmark")
	$(call timed_make,"xdist-benchmark: loadfile",PYTEST_ARGS="$(XDIST_BENCHMARK_TARGETS)" PYTEST_TARGETED_WORKERS="$(PYTEST_WORKERS)" PYTEST_XDIST_DIST=loadfile _test-targeted)
	$(call timed_make,"xdist-benchmark: load",PYTEST_ARGS="$(XDIST_BENCHMARK_TARGETS)" PYTEST_TARGETED_WORKERS="$(PYTEST_WORKERS)" PYTEST_XDIST_DIST=load _test-targeted)
	$(call log_success,"xdist benchmark passed")

precommit: ## Public commit gate using repo-local checks in parallel
	$(call timed_make,"precommit: total",_precommit-impl PYTEST_WORKERS="$(PYTEST_PRECOMMIT_WORKERS)")

_precommit-impl:
ifneq ($(strip $(PYTEST_ARGS)),)
	$(call timed_make,"precommit: targeted tests",_test-targeted)
else
	$(call timed_make,"precommit: unit and journey",$(call parallel_make_args,$(PRECOMMIT_JOBS)) _precommit-unit-fanout _precommit-journey-fanout)
endif
	$(call log_success,"Precommit gate passed")

_precommit-unit-fanout:
	$(call timed_make,"precommit child: unit",test-unit PYTEST_WORKERS="$(PYTEST_PRECOMMIT_WORKERS)" RUNTIME_ROOT="$(TEST_RUNTIME_ROOT)/precommit-unit" LIVE_CONFIG_PATH="$(LIVE_CONFIG_PATH)")

_precommit-journey-fanout:
	$(call timed_make,"precommit child: journey",test-journey PYTEST_WORKERS="$(PYTEST_PRECOMMIT_WORKERS)" RUNTIME_ROOT="$(TEST_RUNTIME_ROOT)/precommit-journey" LIVE_CONFIG_PATH="$(LIVE_CONFIG_PATH)")

quality-gate: test-cov ## Public quality gate using repo-local tests and coverage
	$(call log_success,"Quality gate passed")
