.PHONY: runtime-layout doctor broker-secrets-sync broker-start broker-stop broker-status broker-wait broker-reap broker-smoke tools-count facade-smoke codex-facade-smoke claude-facade-smoke agy-facade-smoke profile-validation codex-profile-validation claude-profile-validation agy-profile-validation discovery-parity codex-claude-discovery-parity codex-deferred-acceptance secret-import-env deployment-stage deployment-rollback deployment-recover governance-pull governance-apply governance-rollback governance-rollout-control fleet-status-collect break-glass-create break-glass-status service-plan launchagent-install launchagent-load launchagent-uninstall launchagent-unload systemd-install systemd-load systemd-uninstall systemd-unload windows-install windows-load windows-unload linux-container-smoke linux-release-gate windows-powershell-smoke config-backup config-render codex-app-policy project-mcp-audit project-mcp-migrate config-rollback profile-snippet

runtime-layout: ## Create configured runtime directories
	$(call log_step,"Runtime layout")
	@MCP_BROKER_RUNTIME_ROOT="$(RUNTIME_ROOT)" "$(ROOT)/scripts/doctor.sh"

doctor: deps runtime-layout broker-reap ## Verify runtime directories and report broker-owned leftovers
	@test -f "$(CONFIG_PATH)" || { $(call log_error,"Missing CONFIG_PATH=$(CONFIG_PATH)"); exit 1; }
	@PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.doctor --config "$(CONFIG_PATH)"
	$(call log_success,"Runtime layout ready at $(RUNTIME_ROOT)")

broker-secrets-sync: runtime-layout ## Import every store-managed upstream secret from the current environment
	@PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.secrets_sync --config "$(CONFIG_PATH)"

broker-start: runtime-layout broker-secrets-sync ## Start broker daemon in the foreground
	@PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.daemon serve --runtime-root "$(RUNTIME_ROOT)" --socket-path "$(SOCKET_PATH)" --config "$(CONFIG_PATH)"

broker-stop: runtime-layout ## Request broker daemon shutdown
	@PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.daemon stop --runtime-root "$(RUNTIME_ROOT)" --socket-path "$(SOCKET_PATH)"

broker-status: runtime-layout ## Query broker daemon health
	@PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.daemon status --runtime-root "$(RUNTIME_ROOT)" --socket-path "$(SOCKET_PATH)"

broker-wait: runtime-layout ## Wait until the broker daemon accepts health checks
	@deadline=$$((SECONDS + $(BROKER_WAIT_SECONDS))); \
	while (( SECONDS <= deadline )); do \
		if PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.daemon status --runtime-root "$(RUNTIME_ROOT)" --socket-path "$(SOCKET_PATH)" >/dev/null 2>&1; then \
			printf "\033[1;32m[OK]\033[0m Broker ready: %s\n" "$(SOCKET_PATH)"; \
			exit 0; \
		fi; \
		sleep 1; \
	done; \
	printf "\033[1;31m[ERROR]\033[0m Broker did not become ready within %s seconds: %s\n" "$(BROKER_WAIT_SECONDS)" "$(SOCKET_PATH)" >&2; \
	exit 1

broker-reap: deps runtime-layout ## Reap stale broker-owned pidfiles, sockets, and orphaned process groups
	@PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.runtime_reaper --runtime-root "$(RUNTIME_ROOT)"

broker-smoke: runtime-layout ## Run broker wiring safety smoke without writing client configs
	@$(MAKE) --no-print-directory config-render CLIENT=codex CONFIG_RENDER_APPLY=0 RUNTIME_ROOT="$(RUNTIME_ROOT)" SOCKET_PATH="$(SOCKET_PATH)"
	@$(MAKE) --no-print-directory config-render CLIENT=claude CONFIG_RENDER_APPLY=0 RUNTIME_ROOT="$(RUNTIME_ROOT)" SOCKET_PATH="$(SOCKET_PATH)"
	@$(MAKE) --no-print-directory config-render CLIENT=agy CONFIG_RENDER_APPLY=0 RUNTIME_ROOT="$(RUNTIME_ROOT)" SOCKET_PATH="$(SOCKET_PATH)"
	@$(MAKE) --no-print-directory broker-reap RUNTIME_ROOT="$(RUNTIME_ROOT)" SOCKET_PATH="$(SOCKET_PATH)"
	$(call log_success,"Broker smoke passed")

tools-count: runtime-layout broker-reap ## Count broker-advertised tools for PROFILE
	@PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.tool_count --config "$(CONFIG_PATH)" --profile "$(PROFILE)"

facade-smoke: runtime-layout broker-reap ## Exercise compact broker facade through the client shim for PROFILE
	@PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.facade_smoke \
		--config "$(CONFIG_PATH)" \
		--profile "$(PROFILE)" \
		--query "$(FACADE_QUERY)" \
		--call-tool "$(FACADE_CALL_TOOL)" \
		--call-args '$(FACADE_CALL_ARGS)' \
		--request-timeout-seconds "$(FACADE_REQUEST_TIMEOUT_SECONDS)"

codex-facade-smoke: PROFILE=codex
codex-facade-smoke: facade-smoke ## Exercise compact Codex broker facade through the client shim

claude-facade-smoke: PROFILE=claude
claude-facade-smoke: facade-smoke ## Exercise compact Claude broker facade through the client shim without wiring Claude

agy-facade-smoke: PROFILE=agy
agy-facade-smoke: facade-smoke ## Exercise compact AGY broker facade profile

profile-validation: runtime-layout broker-reap ## Validate all enabled upstreams for PROFILE using YAML smoke probes
	@PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.profile_validation \
		--config "$(CONFIG_PATH)" \
		--profile "$(PROFILE)"

codex-profile-validation: PROFILE=codex
codex-profile-validation: profile-validation ## Validate every configured Codex upstream through the broker

claude-profile-validation: PROFILE=claude
claude-profile-validation: profile-validation ## Validate every configured Claude upstream without rendering Claude

agy-profile-validation: PROFILE=agy
agy-profile-validation: profile-validation ## Validate every configured AGY upstream through the broker

discovery-parity: runtime-layout broker-reap ## Compare broker discovery between PARITY_LEFT_PROFILE and PARITY_RIGHT_PROFILE
	@PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.discovery_parity \
		--config "$(CONFIG_PATH)" \
		--left-profile "$(PARITY_LEFT_PROFILE)" \
		--right-profile "$(PARITY_RIGHT_PROFILE)" \
		--query "$(DISCOVERY_QUERY)" \
		--call-tool "$(DISCOVERY_CALL_TOOL)" \
		--call-args '$(DISCOVERY_CALL_ARGS)'

codex-claude-discovery-parity: PARITY_LEFT_PROFILE=codex
codex-claude-discovery-parity: PARITY_RIGHT_PROFILE=claude
codex-claude-discovery-parity: discovery-parity ## Compare Codex and Claude broker discovery without wiring Claude

codex-deferred-acceptance: PROFILE=codex
codex-deferred-acceptance: runtime-layout
	@PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.deferred_acceptance \
		--config "$(CONFIG_PATH)" \
		--profile "$(PROFILE)" \
		--format "$(DEFERRED_ACCEPTANCE_FORMAT)"

secret-import-env: runtime-layout ## Store SECRET_NAME from current environment under runtime secrets
	@test -n "$(SECRET_NAME)" || { $(call log_error,"Set SECRET_NAME"); exit 1; }
	@[[ "$(SECRET_NAME)" =~ ^[A-Za-z_][A-Za-z0-9_]*$$ ]] || { $(call log_error,"SECRET_NAME must be an environment variable name"); exit 1; }
	@VALUE="$$(printenv "$(SECRET_NAME)")"; \
		test -n "$$VALUE" || { $(call log_error,"Environment variable $(SECRET_NAME) is missing or empty"); exit 1; }; \
		umask 077; \
		printf '%s\n' "$$VALUE" > "$(SECRETS_DIR)/$(SECRET_NAME)"
	$(call log_success,"Imported $(SECRET_NAME) into $(SECRETS_DIR)")

deployment-stage: runtime-layout bundle-validate ## Validate and stage BUNDLE into transactional deployment state
	@if [[ "$(DEPLOYMENT_DRY_RUN)" == "1" ]]; then \
		DRY_RUN_ARG="--dry-run"; \
	else \
		DRY_RUN_ARG=""; \
	fi; \
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.cli deployment stage \
		--bundle "$(BUNDLE)" \
		--state-dir "$(STATE_DIR)" \
		$$DRY_RUN_ARG
	$(call log_success,"Deployment stage completed")

deployment-rollback: runtime-layout ## Roll back active deployment state to previous
	@PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.cli deployment rollback --state-dir "$(STATE_DIR)"
	$(call log_success,"Deployment rollback completed")

deployment-recover: runtime-layout ## Recover deployment state after partial writes
	@PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.cli deployment recover --state-dir "$(STATE_DIR)"
	$(call log_success,"Deployment recovery completed")

governance-pull: runtime-layout ## Fetch an assigned governance bundle into local cache
	@test -n "$(GOVERNANCE_SOURCE)" || { $(call log_error,"Set GOVERNANCE_SOURCE"); exit 2; }
	@test -n "$(GOVERNANCE_ASSIGNMENT_DECISION)" || { $(call log_error,"Set GOVERNANCE_ASSIGNMENT_DECISION"); exit 2; }
	@test -f "$(GOVERNANCE_ASSIGNMENT_DECISION)" || { $(call log_error,"Missing GOVERNANCE_ASSIGNMENT_DECISION=$(GOVERNANCE_ASSIGNMENT_DECISION)"); exit 1; }
	@test -n "$(GOVERNANCE_AUTH_REF)" || { $(call log_error,"Set GOVERNANCE_AUTH_REF"); exit 2; }
	@if [[ "$(GOVERNANCE_AUTH_PRESENT)" == "1" ]]; then \
		AUTH_PRESENT_ARG="--auth-present"; \
	else \
		AUTH_PRESENT_ARG=""; \
	fi; \
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.cli governance pull \
		--source "$(GOVERNANCE_SOURCE)" \
		--assignment-decision "$(GOVERNANCE_ASSIGNMENT_DECISION)" \
		--state-dir "$(STATE_DIR)" \
		--auth-ref "$(GOVERNANCE_AUTH_REF)" \
		$$AUTH_PRESENT_ARG
	$(call log_success,"Governance pull completed")

governance-apply: runtime-layout ## Apply a cached governance bundle after local approval
	@test -n "$(GOVERNANCE_PULL_RECORD)" || { $(call log_error,"Set GOVERNANCE_PULL_RECORD"); exit 2; }
	@test -f "$(GOVERNANCE_PULL_RECORD)" || { $(call log_error,"Missing GOVERNANCE_PULL_RECORD=$(GOVERNANCE_PULL_RECORD)"); exit 1; }
	@test -n "$(GOVERNANCE_APPROVAL)" || { $(call log_error,"Set GOVERNANCE_APPROVAL"); exit 2; }
	@test -f "$(GOVERNANCE_APPROVAL)" || { $(call log_error,"Missing GOVERNANCE_APPROVAL=$(GOVERNANCE_APPROVAL)"); exit 1; }
	@PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.cli governance apply \
		--pull-record "$(GOVERNANCE_PULL_RECORD)" \
		--state-dir "$(STATE_DIR)" \
		--approval "$(GOVERNANCE_APPROVAL)"
	$(call log_success,"Governance apply completed")

governance-rollback: runtime-layout ## Roll back active governance deployment state
	@PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.cli governance rollback --state-dir "$(STATE_DIR)"
	$(call log_success,"Governance rollback completed")

governance-rollout-control: runtime-layout ## Record local rollout-control audit actions from simulation output
	@test -n "$(GOVERNANCE_ROLLOUT_SIMULATION)" || { $(call log_error,"Set GOVERNANCE_ROLLOUT_SIMULATION"); exit 2; }
	@test -f "$(GOVERNANCE_ROLLOUT_SIMULATION)" || { $(call log_error,"Missing GOVERNANCE_ROLLOUT_SIMULATION=$(GOVERNANCE_ROLLOUT_SIMULATION)"); exit 1; }
	@test -n "$(GOVERNANCE_ROLLOUT_OPERATOR)" || { $(call log_error,"Set GOVERNANCE_ROLLOUT_OPERATOR"); exit 2; }
	@test -n "$(GOVERNANCE_ROLLOUT_BUNDLE_ID)" || { $(call log_error,"Set GOVERNANCE_ROLLOUT_BUNDLE_ID"); exit 2; }
	@test -n "$(GOVERNANCE_ROLLOUT_BUNDLE_VERSION)" || { $(call log_error,"Set GOVERNANCE_ROLLOUT_BUNDLE_VERSION"); exit 2; }
	@test -n "$(GOVERNANCE_ROLLOUT_BUNDLE_CHANNEL)" || { $(call log_error,"Set GOVERNANCE_ROLLOUT_BUNDLE_CHANNEL"); exit 2; }
	@test -n "$(GOVERNANCE_ROLLOUT_BUNDLE_DIGEST)" || { $(call log_error,"Set GOVERNANCE_ROLLOUT_BUNDLE_DIGEST"); exit 2; }
	@if [[ -n "$(GOVERNANCE_ROLLOUT_CREATED_AT)" ]]; then \
		CREATED_AT_ARG="--created-at $(GOVERNANCE_ROLLOUT_CREATED_AT)"; \
	else \
		CREATED_AT_ARG=""; \
	fi; \
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.cli governance rollout-control \
		--simulation "$(GOVERNANCE_ROLLOUT_SIMULATION)" \
		--state-dir "$(STATE_DIR)" \
		--operator "$(GOVERNANCE_ROLLOUT_OPERATOR)" \
		--bundle-id "$(GOVERNANCE_ROLLOUT_BUNDLE_ID)" \
		--bundle-version "$(GOVERNANCE_ROLLOUT_BUNDLE_VERSION)" \
		--bundle-channel "$(GOVERNANCE_ROLLOUT_BUNDLE_CHANNEL)" \
		--bundle-digest "$(GOVERNANCE_ROLLOUT_BUNDLE_DIGEST)" \
		$$CREATED_AT_ARG
	$(call log_success,"Governance rollout-control completed")

fleet-status-collect: runtime-layout ## Prepare a redacted fleet-status collection envelope without uploading
	@test -n "$(FLEET_STATUS_TARGET)" || { $(call log_error,"Set FLEET_STATUS_TARGET"); exit 2; }
	@test -n "$(FLEET_STATUS_AUTH_REF)" || { $(call log_error,"Set FLEET_STATUS_AUTH_REF"); exit 2; }
	@test -f "$(FLEET_STATUS_FILE)" || { $(call log_error,"Missing FLEET_STATUS_FILE=$(FLEET_STATUS_FILE)"); exit 1; }
	@PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.cli fleet-status collect \
		--status-file "$(FLEET_STATUS_FILE)" \
		--target-url "$(FLEET_STATUS_TARGET)" \
		--auth-ref "$(FLEET_STATUS_AUTH_REF)" \
		--retention-days "$(FLEET_STATUS_RETENTION_DAYS)" \
		--collector-id "$(FLEET_STATUS_COLLECTOR_ID)"

break-glass-create: runtime-layout ## Create an expiring break-glass audit record
	@test -n "$(BREAK_GLASS_REASON)" || { $(call log_error,"Set BREAK_GLASS_REASON"); exit 2; }
	@test -n "$(BREAK_GLASS_OPERATOR)" || { $(call log_error,"Set BREAK_GLASS_OPERATOR"); exit 2; }
	@test -n "$(BREAK_GLASS_EXPIRES_AT)" || { $(call log_error,"Set BREAK_GLASS_EXPIRES_AT"); exit 2; }
	@test -n "$(BREAK_GLASS_BYPASS_POLICY_PATHS)" || { $(call log_error,"Set BREAK_GLASS_BYPASS_POLICY_PATHS"); exit 2; }
	@POLICY_ARGS=(); \
	for POLICY_PATH in $(BREAK_GLASS_BYPASS_POLICY_PATHS); do \
		POLICY_ARGS+=(--bypass-policy "$$POLICY_PATH"); \
	done; \
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.cli break-glass create \
		--state-dir "$(STATE_DIR)" \
		--reason "$(BREAK_GLASS_REASON)" \
		--operator "$(BREAK_GLASS_OPERATOR)" \
		--expires-at "$(BREAK_GLASS_EXPIRES_AT)" \
		"$${POLICY_ARGS[@]}"

break-glass-status: runtime-layout ## Report active break-glass status
	@PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.cli break-glass status --state-dir "$(STATE_DIR)"

service-plan: runtime-layout ## Print a non-mutating service bootstrap plan for SERVICE_PLAN_PLATFORM
	@test -n "$(MCP_BROKER_DAEMON_COMMAND)" || { $(call log_error,"Set MCP_BROKER_DAEMON_COMMAND"); exit 2; }
	@PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.cli service plan \
		--platform "$(SERVICE_PLAN_PLATFORM)" \
		--runtime-root "$(RUNTIME_ROOT)" \
		--socket-path "$(SOCKET_PATH)" \
		--config "$(CONFIG_PATH)" \
		--daemon-command "$(MCP_BROKER_DAEMON_COMMAND)" \
		--home-dir "$(SERVICE_PLAN_HOME_DIR)"

launchagent-install: deps runtime-layout ## Render LaunchAgent by default; set LAUNCHAGENT_APPLY=1 to write it
	@if [[ "$(LAUNCHAGENT_APPLY)" == "1" ]]; then \
		APPLY_ARG="--apply"; \
	else \
		APPLY_ARG="--dry-run"; \
	fi; \
	MCP_BROKER_RUNTIME_ROOT="$(RUNTIME_ROOT)" MCP_BROKER_SOCKET="$(SOCKET_PATH)" MCP_BROKER_CONFIG="$(CONFIG_PATH)" "$(ROOT)/scripts/install-launchagent.sh" $$APPLY_ARG

launchagent-load: deps runtime-layout ## Load and kickstart the installed broker LaunchAgent
	@test -f "$(LAUNCHAGENT_PLIST)" || { $(call log_error,"Missing LaunchAgent plist: $(LAUNCHAGENT_PLIST)"); exit 1; }
	@launchctl bootout "$(LAUNCHAGENT_DOMAIN)" "$(LAUNCHAGENT_PLIST)" >/dev/null 2>&1 || true
	@launchctl bootstrap "$(LAUNCHAGENT_DOMAIN)" "$(LAUNCHAGENT_PLIST)"
	@launchctl enable "$(LAUNCHAGENT_DOMAIN)/$(LAUNCHAGENT_LABEL)"
	@launchctl kickstart -k "$(LAUNCHAGENT_DOMAIN)/$(LAUNCHAGENT_LABEL)"
	@$(MAKE) --no-print-directory broker-wait RUNTIME_ROOT="$(RUNTIME_ROOT)" SOCKET_PATH="$(SOCKET_PATH)" BROKER_WAIT_SECONDS="$(BROKER_WAIT_SECONDS)"
	$(call log_success,"LaunchAgent loaded: $(LAUNCHAGENT_LABEL)")

launchagent-uninstall: runtime-layout ## Plan LaunchAgent removal by default; set LAUNCHAGENT_APPLY=1 to remove it
	@if [[ "$(LAUNCHAGENT_APPLY)" == "1" ]]; then \
		APPLY_ARG="--apply"; \
	else \
		APPLY_ARG="--dry-run"; \
	fi; \
	MCP_BROKER_RUNTIME_ROOT="$(RUNTIME_ROOT)" MCP_BROKER_SOCKET="$(SOCKET_PATH)" "$(ROOT)/scripts/uninstall-launchagent.sh" $$APPLY_ARG

launchagent-unload: runtime-layout ## Unload the broker LaunchAgent from launchd
	@launchctl bootout "$(LAUNCHAGENT_DOMAIN)" "$(LAUNCHAGENT_PLIST)" >/dev/null 2>&1 || true
	$(call log_success,"LaunchAgent unloaded: $(LAUNCHAGENT_LABEL)")

systemd-install: deps runtime-layout ## Render systemd user service by default; set SYSTEMD_APPLY=1 to write it
	@if [[ "$(SYSTEMD_APPLY)" == "1" ]]; then \
		APPLY_ARG="--apply"; \
	else \
		APPLY_ARG="--dry-run"; \
	fi; \
	MCP_BROKER_RUNTIME_ROOT="$(RUNTIME_ROOT)" MCP_BROKER_SOCKET="$(SOCKET_PATH)" MCP_BROKER_CONFIG="$(CONFIG_PATH)" MCP_BROKER_DAEMON_COMMAND="$(MCP_BROKER_DAEMON_COMMAND)" MCP_BROKER_SYSTEMD_SERVICE="$(SYSTEMD_SERVICE)" "$(ROOT)/scripts/install-systemd-user.sh" $$APPLY_ARG

systemd-load: deps runtime-layout ## Reload and start the installed systemd user service
	@test -f "$(SYSTEMD_SERVICE_PATH)" || { $(call log_error,"Missing systemd user service: $(SYSTEMD_SERVICE_PATH)"); exit 1; }
	@command -v systemctl >/dev/null 2>&1 || { $(call log_error,"systemctl is required for systemd-load"); exit 1; }
	@systemctl --user daemon-reload
	@systemctl --user enable --now "$(SYSTEMD_SERVICE)"
	$(call log_success,"systemd user service loaded: $(SYSTEMD_SERVICE)")

systemd-uninstall: runtime-layout ## Plan systemd user-service removal by default; set SYSTEMD_APPLY=1 to remove it
	@if [[ "$(SYSTEMD_APPLY)" == "1" ]]; then \
		APPLY_ARG="--apply"; \
	else \
		APPLY_ARG="--dry-run"; \
	fi; \
	MCP_BROKER_RUNTIME_ROOT="$(RUNTIME_ROOT)" MCP_BROKER_SYSTEMD_SERVICE="$(SYSTEMD_SERVICE)" "$(ROOT)/scripts/uninstall-systemd-user.sh" $$APPLY_ARG

systemd-unload: runtime-layout ## Stop and disable the systemd user service
	@command -v systemctl >/dev/null 2>&1 || { $(call log_error,"systemctl is required for systemd-unload"); exit 1; }
	@systemctl --user disable --now "$(SYSTEMD_SERVICE)" >/dev/null 2>&1 || true
	@systemctl --user daemon-reload
	$(call log_success,"systemd user service unloaded: $(SYSTEMD_SERVICE)")

windows-install: runtime-layout ## Render Windows Scheduled Task by default; set WINDOWS_APPLY=1 to register it
	@if [[ "$(WINDOWS_APPLY)" == "1" ]]; then \
		APPLY_ARG="-Apply"; \
	else \
		APPLY_ARG="-DryRun"; \
	fi; \
	if command -v pwsh >/dev/null 2>&1; then \
		PWSH=pwsh; \
	elif command -v powershell.exe >/dev/null 2>&1; then \
		PWSH=powershell.exe; \
	else \
		$(call log_error,"PowerShell is required for windows-install"); exit 1; \
	fi; \
	MCP_BROKER_RUNTIME_ROOT="$(RUNTIME_ROOT)" MCP_BROKER_SOCKET="$(SOCKET_PATH)" MCP_BROKER_CONFIG="$(CONFIG_PATH)" MCP_BROKER_DAEMON_COMMAND="$(MCP_BROKER_DAEMON_COMMAND)" MCP_BROKER_WINDOWS_TASK="$(WINDOWS_TASK)" $$PWSH -NoProfile -ExecutionPolicy Bypass -File "$(ROOT)/scripts/install-windows-task.ps1" $$APPLY_ARG

windows-load: WINDOWS_APPLY=1
windows-load: windows-install ## Register and enable the Windows Scheduled Task
	$(call log_success,"Windows Scheduled Task registered: $(WINDOWS_TASK)")

windows-uninstall: runtime-layout ## Plan Windows Scheduled Task removal by default; set WINDOWS_APPLY=1 to remove it
	@if [[ "$(WINDOWS_APPLY)" == "1" ]]; then \
		APPLY_ARG="-Apply"; \
	else \
		APPLY_ARG="-DryRun"; \
	fi; \
	if command -v pwsh >/dev/null 2>&1; then \
		PWSH=pwsh; \
	elif command -v powershell.exe >/dev/null 2>&1; then \
		PWSH=powershell.exe; \
	else \
		$(call log_error,"PowerShell is required for windows-uninstall"); exit 1; \
	fi; \
	MCP_BROKER_RUNTIME_ROOT="$(RUNTIME_ROOT)" MCP_BROKER_WINDOWS_TASK="$(WINDOWS_TASK)" $$PWSH -NoProfile -ExecutionPolicy Bypass -File "$(ROOT)/scripts/uninstall-windows-task.ps1" $$APPLY_ARG

windows-unload: WINDOWS_APPLY=1
windows-unload: windows-uninstall ## Remove the Windows Scheduled Task

linux-container-smoke: ## Run public setup and systemd dry-run inside a Linux container
	@MCP_BROKER_LINUX_SMOKE_IMAGE="$(LINUX_SMOKE_IMAGE)" "$(ROOT)/scripts/linux-container-smoke.sh"

linux-release-gate: ## Run the PyPI workflow release gate inside Linux
	@MCP_BROKER_LINUX_RELEASE_GATE_IMAGE="$(LINUX_RELEASE_GATE_IMAGE)" "$(ROOT)/scripts/linux-release-gate.sh"

windows-powershell-smoke: ## Run Windows Scheduled Task PowerShell dry-run checks
	@"$(ROOT)/scripts/windows-powershell-smoke.sh"

config-backup: runtime-layout ## Back up one configured client target without rendering or applying config
	@if [[ -n "$(CONFIG_BACKUP_LABEL)" ]]; then \
		LABEL_ARG="--label $(CONFIG_BACKUP_LABEL)"; \
	else \
		LABEL_ARG=""; \
	fi; \
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.config_render backup --config "$(CONFIG_PATH)" --client "$(CLIENT)" $$LABEL_ARG

config-render: runtime-layout ## Render one client config to runtime renders; set CONFIG_RENDER_APPLY=1 to write target
	@if [[ "$(CONFIG_RENDER_APPLY)" == "1" ]]; then \
		APPLY_ARG="--apply"; \
	else \
		APPLY_ARG=""; \
	fi; \
	if [[ -n "$(CONFIG_RENDER_TARGET_PATH)" ]]; then \
		TARGET_PATH_ARG=(--target-path "$(CONFIG_RENDER_TARGET_PATH)"); \
	else \
		TARGET_PATH_ARG=(); \
	fi; \
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.config_render render --config "$(CONFIG_PATH)" --client "$(CLIENT)" $$APPLY_ARG "$${TARGET_PATH_ARG[@]}"

codex-app-policy: runtime-layout ## Enforce configured Codex app connector policy; set CODEX_APP_POLICY_APPLY=1 to write cache changes
	@if [[ "$(CODEX_APP_POLICY_APPLY)" == "1" ]]; then \
		APPLY_ARG="--apply"; \
	else \
		APPLY_ARG=""; \
	fi; \
	if [[ -n "$(CONFIG_BACKUP_LABEL)" ]]; then \
		LABEL_ARG="--label $(CONFIG_BACKUP_LABEL)"; \
	else \
		LABEL_ARG=""; \
	fi; \
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.config_render app-policy --config "$(CONFIG_PATH)" --client "$(CLIENT)" $$APPLY_ARG $$LABEL_ARG

project-mcp-audit: runtime-layout ## Audit project .mcp.json files against broker config
	@ROOT_ARGS=(); \
	for root in $(PROJECT_MCP_ROOTS); do ROOT_ARGS+=(--root "$$root"); done; \
	PROFILE_ARGS=(); \
	for profile in $(PROJECT_MCP_PROFILES); do PROFILE_ARGS+=(--profile "$$profile"); done; \
	if [[ -n "$(PROJECT_MCP_CLAUDE_CONFIG)" && -f "$(PROJECT_MCP_CLAUDE_CONFIG)" ]]; then CLAUDE_ARGS=(--claude-config "$(PROJECT_MCP_CLAUDE_CONFIG)"); else CLAUDE_ARGS=(); fi; \
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.project_mcp \
		--config "$(CONFIG_PATH)" \
		--backup-root "$(PROJECT_MCP_BACKUP_ROOT)" \
		"$${ROOT_ARGS[@]}" \
		"$${PROFILE_ARGS[@]}" \
		"$${CLAUDE_ARGS[@]}"

project-mcp-migrate: runtime-layout ## Back up and empty covered project .mcp.json files; set PROJECT_MCP_IMPORT_MISSING=1 to import missing entries
	@ROOT_ARGS=(); \
	for root in $(PROJECT_MCP_ROOTS); do ROOT_ARGS+=(--root "$$root"); done; \
	PROFILE_ARGS=(); \
	for profile in $(PROJECT_MCP_PROFILES); do PROFILE_ARGS+=(--profile "$$profile"); done; \
	if [[ "$(PROJECT_MCP_APPLY)" == "1" ]]; then APPLY_ARG="--apply"; else APPLY_ARG=""; fi; \
	if [[ "$(PROJECT_MCP_IMPORT_MISSING)" == "1" ]]; then IMPORT_ARG="--import-missing"; else IMPORT_ARG=""; fi; \
	if [[ -n "$(PROJECT_MCP_CLAUDE_CONFIG)" && -f "$(PROJECT_MCP_CLAUDE_CONFIG)" ]]; then CLAUDE_ARGS=(--claude-config "$(PROJECT_MCP_CLAUDE_CONFIG)"); else CLAUDE_ARGS=(); fi; \
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.project_mcp \
		--config "$(CONFIG_PATH)" \
		--backup-root "$(PROJECT_MCP_BACKUP_ROOT)" \
		"$${ROOT_ARGS[@]}" \
		"$${PROFILE_ARGS[@]}" \
		"$${CLAUDE_ARGS[@]}" \
		$$APPLY_ARG $$IMPORT_ARG

config-rollback: runtime-layout ## Restore latest backup for one client config
	@PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.config_render rollback --config "$(CONFIG_PATH)" --client "$(CLIENT)"

profile-snippet: ## Print a generic profile/client YAML snippet; set NEW_PROFILE and NEW_CLIENT_FORMAT
	@PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m mcp_broker.profile_snippet \
		--profile "$(NEW_PROFILE)" \
		--client-format "$(NEW_CLIENT_FORMAT)" \
		--config-path '$(NEW_CLIENT_CONFIG_PATH)' \
		--entry-name "$(NEW_CLIENT_ENTRY_NAME)" \
		--command "$(NEW_CLIENT_COMMAND)" \
		--broker-tool-name-style "$(NEW_BROKER_TOOL_NAME_STYLE)"
