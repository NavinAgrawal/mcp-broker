.PHONY: release-smoke package-build package-check package-install-smoke public-stable-surface-smoke public-release-surface-smoke public-release-live-verify npm-account-check npm-package-check npm-smoke npm-release-smoke docker-build docker-smoke docker-buildx _docker-release-builder-ensure docker-mcp-catalog-smoke docker-hub-public-ensure docker-publish-check docker-release-smoke mcpb-validate mcpb-pack mcpb-smoke mcpb-stdio-smoke smithery-payload-check smithery-publish directory-submission-check release-version-resolve release-version-sync release-version-check release-check _release-check-impl release _release-impl publish-version-check publish-everywhere-check _publish-everywhere-check-impl publish-everywhere _publish-everywhere-impl _publish-everywhere-preflight _publish-check-docker-smoke _publish-check-docker-buildx _publish-everywhere-required-env-check _publish-everywhere-auth-preflight _publish-everywhere-docker-hub-public _publish-everywhere-registry-fanout _publish-everywhere-live-verify-registries _publish-everywhere-github-release _publish-everywhere-live-verify-github-release _publish-everywhere-pypi _publish-everywhere-npm _publish-everywhere-docker-auth _publish-everywhere-docker _publish-everywhere-mcp-registry _publish-everywhere-homebrew

release-smoke: ## Run clean-tree public setup smoke from tracked files
	@"$(ROOT)/scripts/release-smoke.sh"

package-build: $(VENV_DIR)/.deps.stamp ## Build wheel and source distribution into dist/
	@rm -rf "$(PACKAGE_DIST_DIR)"
	@MCP_BROKER_VERSION="$(PACKAGE_VERSION)" $(PYTHON) -m build --outdir "$(PACKAGE_DIST_DIR)" "$(ROOT)"
	$(call log_success,"Package artifacts built in $(PACKAGE_DIST_DIR)")

package-check: package-build ## Validate built package metadata
	@$(PYTHON) -m twine check "$(PACKAGE_DIST_DIR)"/*
	$(call log_success,"Package artifacts passed twine check")

package-install-smoke: ## Validate pipx and uvx against the published stable PyPI package
	@command -v pipx >/dev/null 2>&1 || { printf "\033[1;31m[ERROR]\033[0m pipx is required for package-install-smoke\n" >&2; exit 2; }
	@command -v "$(UVX)" >/dev/null 2>&1 || { printf "\033[1;31m[ERROR]\033[0m uvx is required for package-install-smoke\n" >&2; exit 2; }
	@tmpdir="$$(mktemp -d)"; \
		trap 'rm -rf "$$tmpdir"' EXIT; \
		PIPX_HOME="$$tmpdir/pipx-home" PIPX_BIN_DIR="$$tmpdir/pipx-bin" \
			pipx run --spec "mcp-broker==$(PACKAGE_INSTALL_VERSION)" mcp-broker --help >/dev/null; \
		UV_TOOL_DIR="$$tmpdir/uv-tools" UV_CACHE_DIR="$$tmpdir/uv-cache" \
			"$(UVX)" --from "mcp-broker==$(PACKAGE_INSTALL_VERSION)" mcp-broker --help >/dev/null
	$(call log_success,"pipx and uvx install smoke passed: mcp-broker $(PACKAGE_INSTALL_VERSION)")

public-stable-surface-smoke: ## Download and verify public stable artifacts from real registries
	@PUBLIC_SURFACE_VERSION="$(PACKAGE_INSTALL_VERSION)" \
		PUBLIC_SURFACE_REQUIRE_NPM=0 \
		PUBLIC_SURFACE_REQUIRE_DOCKER=0 \
		PYPI_PROJECT_NAME="$(PYPI_PROJECT_NAME)" \
		PACKAGE_COMMAND_NAME="$(PACKAGE_COMMAND_NAME)" \
		PACKAGE_CLIENT_COMMAND_NAME="$(PACKAGE_CLIENT_COMMAND_NAME)" \
		PACKAGE_DAEMON_COMMAND_NAME="$(PACKAGE_DAEMON_COMMAND_NAME)" \
		GITHUB_TAG_SOURCE_TARBALL_URL="$(GITHUB_TAG_SOURCE_TARBALL_URL)" \
		HOMEBREW_FORMULA_REF="$(HOMEBREW_FORMULA_REF)" \
		MCP_REGISTRY_NAME="$(MCP_REGISTRY_NAME)" \
		MCP_REGISTRY_SEARCH_URL="$(MCP_REGISTRY_SEARCH_URL)" \
		"$(ROOT)/scripts/public-surface-smoke.sh"

public-release-surface-smoke: ## Download and verify every public release surface after publication
	@PUBLIC_SURFACE_VERSION="$(PACKAGE_VERSION)" \
		PUBLIC_SURFACE_REQUIRE_NPM=1 \
		PUBLIC_SURFACE_REQUIRE_DOCKER=1 \
		PYPI_PROJECT_NAME="$(PYPI_PROJECT_NAME)" \
		PACKAGE_COMMAND_NAME="$(PACKAGE_COMMAND_NAME)" \
		PACKAGE_CLIENT_COMMAND_NAME="$(PACKAGE_CLIENT_COMMAND_NAME)" \
		PACKAGE_DAEMON_COMMAND_NAME="$(PACKAGE_DAEMON_COMMAND_NAME)" \
		GITHUB_TAG_SOURCE_TARBALL_URL="$(GITHUB_TAG_SOURCE_TARBALL_URL)" \
		HOMEBREW_FORMULA_REF="$(HOMEBREW_FORMULA_REF)" \
		MCP_REGISTRY_NAME="$(MCP_REGISTRY_NAME)" \
		MCP_REGISTRY_SEARCH_URL="$(MCP_REGISTRY_SEARCH_URL)" \
		NPM_PACKAGE_NAME="$(NPM_PACKAGE_NAME)" \
		DOCKER_RELEASE_IMAGE="$(DOCKER_RELEASE_IMAGE)" \
		"$(ROOT)/scripts/public-surface-smoke.sh"

public-release-live-verify: ## Verify public registry APIs and release object for the current release
	@$(PYTHON_BIN) "$(ROOT)/scripts/verify_public_release.py" \
		--version "$(PACKAGE_VERSION)" \
		--npm-package "$(NPM_PACKAGE_NAME)" \
		--docker-namespace "$(DOCKER_NAMESPACE)" \
		--docker-image-name "$(DOCKER_IMAGE_NAME)" \
		--mcp-registry-name "$(MCP_REGISTRY_NAME)" \
		--github-release-url "$(GITHUB_RELEASE_URL)" \
		--pypi-version-url "$(PYPI_VERSION_URL)" \
		--npm-registry-url "$(NPM_REGISTRY_URL)" \
		--docker-hub-api-repository-base-url "$(DOCKER_HUB_API_REPOSITORY_BASE_URL)" \
		--docker-registry-service "$(DOCKER_REGISTRY_SERVICE)" \
		--docker-registry-host "$(DOCKER_REGISTRY_HOST)" \
		--docker-registry-auth-url "$(DOCKER_REGISTRY_AUTH_URL)" \
		--docker-registry-manifest-base-url "$(DOCKER_REGISTRY_MANIFEST_BASE_URL)" \
		--ghcr-registry-service "$(GHCR_REGISTRY_SERVICE)" \
		--ghcr-registry-host "$(GHCR_REGISTRY_HOST)" \
		--ghcr-registry-auth-url "$(GHCR_REGISTRY_AUTH_URL)" \
		--ghcr-registry-manifest-base-url "$(GHCR_REGISTRY_MANIFEST_BASE_URL)" \
		--mcp-registry-search-url "$(MCP_REGISTRY_SEARCH_URL)" \
		--homebrew-formula-url "$(HOMEBREW_FORMULA_RAW_URL)"

npm-account-check: ## Verify the maintainer NPM login and scoped package visibility
	@test -d "$(NPM_DIR)" || { printf "\033[1;31m[ERROR]\033[0m Missing NPM package directory: %s\n" "$(NPM_DIR)" >&2; exit 1; }
	@cd "$(NPM_DIR)" && $(NPM) whoami
	@cd "$(NPM_DIR)" && $(NPM) view "$(NPM_PACKAGE_NAME)" --json || true

npm-package-check: ## Validate the scoped NPM package tarball contents
	@test -d "$(NPM_DIR)" || { printf "\033[1;31m[ERROR]\033[0m Missing NPM package directory: %s\n" "$(NPM_DIR)" >&2; exit 1; }
	@cd "$(NPM_DIR)" && $(NPM) pack --dry-run --json
	$(call log_success,"NPM package dry run passed: $(NPM_PACKAGE_NAME)")

npm-smoke: ## Run the local NPM bridge wrapper
	@test -d "$(NPM_DIR)" || { printf "\033[1;31m[ERROR]\033[0m Missing NPM package directory: %s\n" "$(NPM_DIR)" >&2; exit 1; }
	@cd "$(NPM_DIR)" && MCP_BROKER_NPM_DEV_ROOT="$(ROOT)" node bin/mcp-broker.js --help
	$(call log_success,"NPM smoke passed: $(NPM_PACKAGE_NAME)")

npm-release-smoke: ## Verify the published NPM bridge package
	@$(NPM) view "$(NPM_PACKAGE_NAME)" version repository dist-tags --json
	@$(NPX) -y "$(NPM_PACKAGE_NAME)" --help
	$(call log_success,"NPM release smoke passed: $(NPM_PACKAGE_NAME)")

docker-build: ## Build the local Docker image
	$(call log_step,"Building Docker image $(DOCKER_IMAGE)")
	@docker build $(if $(DOCKER_PLATFORM),--platform "$(DOCKER_PLATFORM)",) \
		--build-arg VERSION="$(PACKAGE_VERSION)" \
		--build-arg VCS_REF="$$(git -C "$(ROOT)" rev-parse --short HEAD 2>/dev/null || printf unknown)" \
		--build-arg SOURCE_URL="$(DOCKER_SOURCE_URL)" \
		--build-arg AUTHORS="$(PACKAGE_AUTHOR)" \
		-t "$(DOCKER_IMAGE)" "$(ROOT)"
	$(call log_success,"Docker image built: $(DOCKER_IMAGE)")

docker-smoke: docker-build ## Smoke test the Docker stdio entrypoint
	$(call log_step,"Smoke testing Docker image $(DOCKER_IMAGE)")
	@mkdir -p "$(TEST_LOG_DIR)"
	@printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"docker-smoke","version":"0"}}}\n{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n' | docker run --rm -i "$(DOCKER_IMAGE)" > "$(TEST_LOG_DIR)/docker-smoke.jsonl"
	@grep -q '"tools"' "$(TEST_LOG_DIR)/docker-smoke.jsonl"
	$(call log_success,"Docker smoke passed")

docker-buildx: ## Build multi-arch Docker image with SBOM/provenance; set DOCKER_PUSH=1 to push
	$(call log_step,"Building multi-arch Docker image $(DOCKER_IMAGE)")
	@$(MAKE) --no-print-directory _docker-release-builder-ensure
	@if [[ "$(DOCKER_PUSH)" == "1" ]]; then \
		OUTPUT_ARG="--push"; \
		SBOM_ARG="$(DOCKER_SBOM)"; \
		PROVENANCE_ARG="$(DOCKER_PROVENANCE)"; \
	else \
		OUTPUT_ARG="--load"; \
		SBOM_ARG="false"; \
		PROVENANCE_ARG="false"; \
		if [[ "$(DOCKER_PLATFORMS)" == *,* ]]; then \
			printf "\033[1;31m[ERROR]\033[0m docker-buildx without DOCKER_PUSH=1 supports one platform only; set DOCKER_PLATFORMS=linux/arm64 for local load\n" >&2; \
			exit 1; \
		fi; \
	fi; \
	docker buildx build \
		--builder "$(DOCKER_RELEASE_BUILDER)" \
		--platform "$(DOCKER_PLATFORMS)" \
		--build-arg VERSION="$(PACKAGE_VERSION)" \
		--build-arg VCS_REF="$$(git -C "$(ROOT)" rev-parse --short HEAD 2>/dev/null || printf unknown)" \
		--build-arg SOURCE_URL="$(DOCKER_SOURCE_URL)" \
		--build-arg AUTHORS="$(PACKAGE_AUTHOR)" \
		--sbom=$$SBOM_ARG \
		--provenance=$$PROVENANCE_ARG \
		-t "$(DOCKER_IMAGE)" \
		$$OUTPUT_ARG \
		"$(ROOT)"
	$(call log_success,"Docker buildx completed: $(DOCKER_IMAGE)")

_docker-release-builder-ensure:
	@builder_info="$$(docker buildx inspect "$(DOCKER_RELEASE_BUILDER)" 2>/dev/null)" || { \
		docker buildx create \
			--name "$(DOCKER_RELEASE_BUILDER)" \
			--driver "$(DOCKER_RELEASE_BUILDER_DRIVER)" \
			--bootstrap; \
		builder_info="$$(docker buildx inspect "$(DOCKER_RELEASE_BUILDER)")"; \
	}; \
	builder_driver="$$(printf '%s\n' "$$builder_info" | awk -F ': *' '$$1 == "Driver" { print $$2; exit }')"; \
	if [[ "$$builder_driver" != "$(DOCKER_RELEASE_BUILDER_DRIVER)" ]]; then \
		printf "\033[1;31m[ERROR]\033[0m builder driver mismatch: %s uses %s, required %s\n" \
			"$(DOCKER_RELEASE_BUILDER)" "$${builder_driver:-unknown}" "$(DOCKER_RELEASE_BUILDER_DRIVER)" >&2; \
		exit 2; \
	fi
	@docker buildx inspect --bootstrap "$(DOCKER_RELEASE_BUILDER)" >/dev/null

docker-mcp-catalog-smoke: ## Verify Docker MCP Toolkit can create a custom catalog from file metadata
	@test -f "$(DOCKER_MCP_CATALOG_FILE)" || { printf "\033[1;31m[ERROR]\033[0m Missing Docker MCP catalog file: %s\n" "$(DOCKER_MCP_CATALOG_FILE)" >&2; exit 2; }
	@docker mcp catalog remove "$(DOCKER_MCP_CATALOG_REF)" >/dev/null 2>&1 || true
	@docker mcp catalog create "$(DOCKER_MCP_CATALOG_REF)" \
		--title "mcp-broker local" \
		--server "file://$(DOCKER_MCP_CATALOG_FILE)" >/dev/null
	@trap 'docker mcp catalog remove "$(DOCKER_MCP_CATALOG_REF)" >/dev/null 2>&1 || true' EXIT; \
		docker mcp catalog server ls "$(DOCKER_MCP_CATALOG_REF)" --format json | grep -q '"mcp-broker"'
	$(call log_success,"Docker MCP custom catalog smoke passed: $(DOCKER_MCP_CATALOG_REF)")

docker-hub-public-ensure: ## Ensure Docker Hub repository exists with public visibility
	@test -n "$${DOCKERHUB_USERNAME:-}" || { printf "\033[1;31m[ERROR]\033[0m DOCKERHUB_USERNAME is required for docker-hub-public-ensure\n" >&2; exit 2; }
	@test -n "$${DOCKERHUB_TOKEN:-}" || { printf "\033[1;31m[ERROR]\033[0m DOCKERHUB_TOKEN is required for docker-hub-public-ensure\n" >&2; exit 2; }
	@$(PYTHON_BIN) "$(ROOT)/scripts/ensure_docker_hub_public.py" \
		--username "$${DOCKERHUB_USERNAME}" \
		--namespace "$(DOCKER_NAMESPACE)" \
		--repository "$(DOCKER_IMAGE_NAME)" \
		--registry "$(DOCKER_REGISTRY_HOST)" \
		--registry-auth-url "$(DOCKER_REGISTRY_AUTH_URL)" \
		--registry-service "$(DOCKER_REGISTRY_SERVICE)" \
		--login-url "$(DOCKER_HUB_LOGIN_URL)" \
		--namespace-repositories-url "$(DOCKER_HUB_API_NAMESPACE_BASE_URL)" \
		--legacy-repositories-url "$(DOCKER_HUB_LEGACY_REPOSITORY_BASE_URL)"

docker-publish-check: ## Verify published Docker manifests
	@for image in $(DOCKER_PUBLISH_IMAGES); do \
		docker buildx imagetools inspect "$$image" >/dev/null; \
		printf "\033[1;32m[OK]\033[0m Docker manifest exists: %s\n" "$$image"; \
	done

docker-release-smoke: ## Smoke test the published Docker image
	$(call log_step,"Smoke testing published Docker image $(DOCKER_RELEASE_IMAGE)")
	@mkdir -p "$(TEST_LOG_DIR)"
	@printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"docker-release-smoke","version":"0"}}}\n{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n' | docker run --rm -i "$(DOCKER_RELEASE_IMAGE)" > "$(TEST_LOG_DIR)/docker-release-smoke.jsonl"
	@grep -q '"tools"' "$(TEST_LOG_DIR)/docker-release-smoke.jsonl"
	$(call log_success,"Docker release smoke passed: $(DOCKER_RELEASE_IMAGE)")

mcpb-validate: ## Validate MCPB manifest metadata
	$(call log_step,"Validating MCPB manifest")
	@npx -y @anthropic-ai/mcpb validate "$(MCPB_MANIFEST)"
	$(call log_success,"MCPB manifest passed")

mcpb-pack: mcpb-validate ## Build the MCPB bundle into dist/
	$(call log_step,"Packing MCPB bundle")
	@mkdir -p "$(dir $(MCPB_OUTPUT))"
	@rm -f "$(MCPB_OUTPUT)"
	@$(NPX) -y @anthropic-ai/mcpb pack "$(ROOT)/mcpb" "$(MCPB_OUTPUT)"
	$(call log_success,"MCPB bundle built: $(MCPB_OUTPUT)")

mcpb-smoke: ## Pack, inspect, and unpack the MCPB bundle without installing it
	$(call log_step,"Smoke testing MCPB bundle")
	@rm -rf "$(MCPB_SMOKE_DIR)"
	@mkdir -p "$(MCPB_SMOKE_DIR)"
	@$(NPX) -y @anthropic-ai/mcpb validate "$(MCPB_MANIFEST)"
	@$(NPX) -y @anthropic-ai/mcpb pack "$(ROOT)/mcpb" "$(MCPB_SMOKE_OUTPUT)"
	@$(NPX) -y @anthropic-ai/mcpb info "$(MCPB_SMOKE_OUTPUT)" > "$(MCPB_SMOKE_DIR)/info.txt"
	@$(NPX) -y @anthropic-ai/mcpb unpack "$(MCPB_SMOKE_OUTPUT)" "$(MCPB_SMOKE_UNPACK_DIR)"
	@$(PYTHON_BIN) -c 'import json, pathlib; p=pathlib.Path("$(MCPB_SMOKE_UNPACK_DIR)/manifest.json"); m=json.loads(p.read_text()); assert m["name"]=="mcp-broker"; assert m["server"]["type"]=="binary"; assert m["server"]["mcp_config"]["command"]=="$${user_config.uvx_path}"; assert m["user_config"]["uvx_path"]["required"] is True; assert "broker_status" in {t["name"] for t in m["tools"]}'
	$(call log_success,"MCPB smoke passed: $(MCPB_SMOKE_OUTPUT)")

mcpb-stdio-smoke: mcpb-pack broker-status ## Run the MCPB stdio command shape against an already-running broker
	$(call log_step,"Smoke testing MCPB stdio command")
	@PYTHONPATH="$(PYTHONPATH)" $(PYTHON) "$(ROOT)/scripts/mcpb_stdio_smoke.py" \
		--manifest "$(MCPB_MANIFEST)" \
		--command "$(MCPB_STDIO_COMMAND)" \
		--runtime-root "$(RUNTIME_ROOT)" \
		--socket-path "$(SOCKET_PATH)" \
		--config "$(CONFIG_PATH)" \
		--profile "claude"
	$(call log_success,"MCPB stdio smoke passed")

smithery-payload-check: mcpb-pack ## Build and validate Smithery release payload from the MCPB bundle
	$(call log_step,"Checking Smithery release payload")
	@PYTHONPATH="$(PYTHONPATH)" $(PYTHON) "$(ROOT)/scripts/smithery_release.py" "$(MCPB_OUTPUT)" \
		--name "$(SMITHERY_QUALIFIED_NAME)" \
		--base-url "$(SMITHERY_API_BASE_URL)" \
		--dry-run \
		--payload-output "$(SMITHERY_PAYLOAD_OUTPUT)"
	@$(PYTHON_BIN) -c 'import json, pathlib; p=json.loads(pathlib.Path("$(SMITHERY_PAYLOAD_OUTPUT)").read_text()); assert p["type"]=="stdio"; assert p["runtime"]=="binary"; assert "configSchema" in p; tools=p["serverCard"]["tools"]; assert all(t.get("inputSchema",{}).get("type")=="object" for t in tools); assert {t["name"] for t in tools}>={"broker_search_tools","broker_describe_tool","broker_call_tool","broker_status"}'
	$(call log_success,"Smithery payload check passed: $(SMITHERY_PAYLOAD_OUTPUT)")

smithery-publish: smithery-payload-check ## Publish the MCPB bundle to Smithery using the repo payload adapter
	$(call log_step,"Publishing Smithery MCPB bundle")
	@PYTHONPATH="$(PYTHONPATH)" $(PYTHON) "$(ROOT)/scripts/smithery_release.py" "$(MCPB_OUTPUT)" \
		--name "$(SMITHERY_QUALIFIED_NAME)" \
		--base-url "$(SMITHERY_API_BASE_URL)"
	$(call log_success,"Smithery publish target completed: $(SMITHERY_QUALIFIED_NAME)")

directory-submission-check: mcpb-validate ## Validate directory submission packet, server card, registry metadata, and MCPB manifest
	$(call log_step,"Checking directory submission metadata")
	@DIRECTORY_SUBMISSION_PACKET="$(DIRECTORY_SUBMISSION_PACKET)" \
		SERVER_CARD_PATH="$(SERVER_CARD_PATH)" \
		REGISTRY_METADATA_PATH="$(REGISTRY_METADATA_PATH)" \
		MCPB_MANIFEST="$(MCPB_MANIFEST)" \
		PACKAGE_SLUG="$(PACKAGE_SLUG)" \
		PACKAGE_COMMAND_NAME="$(PACKAGE_COMMAND_NAME)" \
		GITHUB_REPOSITORY_URL="$(GITHUB_REPOSITORY_URL)" \
		PYPI_PROJECT_NAME="$(PYPI_PROJECT_NAME)" \
		GLAMA_MAINTAINER="$(GLAMA_MAINTAINER)" \
		GLAMA_SCHEMA_URL="$(GLAMA_SCHEMA_URL)" \
		GLAMA_LISTING_URL="$(GLAMA_LISTING_URL)" \
		PULSEMCP_LISTING_URL="$(PULSEMCP_LISTING_URL)" \
		PULSEMCP_SUBMIT_URL="$(PULSEMCP_SUBMIT_URL)" \
		MCPSERVERS_LISTING_URL="$(MCPSERVERS_LISTING_URL)" \
		MCP_SO_LISTING_URL="$(MCP_SO_LISTING_URL)" \
		MCPCENTRAL_REGISTRY_URL="$(MCPCENTRAL_REGISTRY_URL)" \
		$(PYTHON_BIN) "$(ROOT)/scripts/check_directory_submission.py"
	$(call log_success,"Directory submission check passed")

release-version-resolve: ## Print the release version resolved from RELEASE_VERSION, RELEASE_BUMP, or package metadata
	@$(PYTHON_BIN) "$(ROOT)/scripts/sync_release_metadata.py" \
		$(if $(RELEASE_VERSION),--version "$(RELEASE_VERSION)",) \
		$(if $(RELEASE_BUMP),--bump "$(RELEASE_BUMP)",) \
		--emit-version

release-version-sync: ## Synchronize release metadata from RELEASE_VERSION or RELEASE_BUMP
	@test -n "$(RELEASE_VERSION)$(RELEASE_BUMP)" || { printf "\033[1;31m[ERROR]\033[0m Set RELEASE_VERSION=<semver> or RELEASE_BUMP=patch|minor|major\n" >&2; exit 2; }
	@$(PYTHON_BIN) "$(ROOT)/scripts/sync_release_metadata.py" \
		$(if $(RELEASE_VERSION),--version "$(RELEASE_VERSION)",) \
		$(if $(RELEASE_BUMP),--bump "$(RELEASE_BUMP)",) \
		--write
	$(call log_success,"Release metadata synchronized")

release-version-check: ## Verify the intended release version is explicit and aligned
	@expected="$(RELEASE_VERSION)"; \
	if [[ -z "$$expected" && "$${GITHUB_REF_NAME:-}" == v* ]]; then expected="$${GITHUB_REF_NAME#v}"; fi; \
	if [[ -z "$$expected" ]]; then \
		printf "\033[1;31m[ERROR]\033[0m Set RELEASE_VERSION=<semver> before running release checks\n" >&2; \
		exit 2; \
	fi; \
	$(PYTHON_BIN) "$(ROOT)/scripts/sync_release_metadata.py" --version "$$expected" --check; \
	EXPECTED_PUBLISH_VERSION="$$expected" GITHUB_REF_NAME="$${GITHUB_REF_NAME:-}" $(PYTHON) "$(ROOT)/scripts/check_release_versions.py"
	$(call log_success,"Release version is explicit and aligned")

release-check: ## Run the full release transaction preflight before tagging or publishing
	$(call timed_make,"release-check: total",_release-check-impl)

_release-check-impl:
	$(call timed_make,"release-check: version",release-version-check)
	$(call timed_make,"release-check: publish preflight",publish-everywhere-check)
	$(call timed_make,"release-check: directory and bundle metadata",$(call parallel_make_args,$(PUBLISH_CHECK_JOBS)) directory-submission-check mcpb-smoke smithery-payload-check)
	$(call log_success,"Release preflight passed")

release: ## Release transaction: preflight once, then publish every public surface
	$(call timed_make,"release: total",_release-impl)

_release-impl:
	@$(MAKE) --no-print-directory _publish-execution-mode-check
	@test "$(RELEASE_APPLY)" = "1" || { printf "\033[1;31m[ERROR]\033[0m Set RELEASE_APPLY=1 to run the release transaction\n" >&2; exit 2; }
	$(call timed_make,"release: preflight",release-check)
	$(call timed_make,"release: publish",PUBLISH_EVERYWHERE_APPLY=1 PUBLISH_EVERYWHERE_SKIP_CHECKS=1 publish-everywhere)
	$(call log_success,"Release transaction completed")

publish-version-check: ## Verify all release metadata versions match
	@EXPECTED_PUBLISH_VERSION="$(EXPECTED_PUBLISH_VERSION)" GITHUB_REF_NAME="$${GITHUB_REF_NAME:-}" $(PYTHON) "$(ROOT)/scripts/check_release_versions.py"
	$(call log_success,"Release versions are aligned")

publish-everywhere-check: ## Run all local gates required before registry publication
	$(call timed_make,"publish-everywhere-check: total",_publish-everywhere-check-impl)

_publish-everywhere-check-impl:
	$(call timed_make,"publish-everywhere-check: version check",publish-version-check)
	$(call timed_make,"publish-everywhere-check: release gate",PYTEST_MARKER_EXPRESSION="$(RELEASE_GATE_PYTEST_MARKER_EXPRESSION)" release-gate)
	$(call timed_make,"publish-everywhere-check: package smoke children",$(call parallel_make_args,$(PUBLISH_CHECK_JOBS)) npm-package-check npm-smoke _publish-check-docker-smoke _publish-check-docker-buildx)
	$(call log_success,"Publish-everywhere checks passed")

_publish-check-docker-smoke:
	$(call timed_make,"publish check child: docker-smoke",docker-smoke DOCKER_IMAGE="mcp-broker:publish-check")

_publish-check-docker-buildx:
	$(call timed_make,"publish check child: docker-buildx",docker-buildx DOCKER_IMAGE="mcp-broker:buildx-check" DOCKER_PLATFORMS="$(DOCKER_LOCAL_PLATFORM)")

publish-everywhere: ## One-shot publication to PyPI, NPM, Docker Hub, GHCR, MCP Registry, and Homebrew
	$(call timed_make,"publish-everywhere: total",_publish-everywhere-impl)

_publish-everywhere-impl:
	@$(MAKE) --no-print-directory _publish-execution-mode-check
	@test "$(PUBLISH_EVERYWHERE_APPLY)" = "1" || { printf "\033[1;31m[ERROR]\033[0m Set PUBLISH_EVERYWHERE_APPLY=1 to publish\n" >&2; exit 2; }
	$(call timed_make,"publish-everywhere: preflight checks",_publish-everywhere-preflight)
	$(call timed_make,"publish-everywhere: required env",_publish-everywhere-required-env-check)
	$(call timed_make,"publish-everywhere: auth preflight",_publish-everywhere-auth-preflight)
	$(call timed_make,"publish-everywhere: docker registry auth",_publish-everywhere-docker-auth)
	$(call timed_make,"publish-everywhere: docker hub public repository",_publish-everywhere-docker-hub-public)
	$(call timed_make,"publish-everywhere: pypi",_publish-everywhere-pypi)
	$(call timed_make,"publish-everywhere: registry fanout",_publish-everywhere-registry-fanout)
	$(call timed_make,"publish-everywhere: live registry verification",_publish-everywhere-live-verify-registries)
	$(call timed_make,"publish-everywhere: github release",_publish-everywhere-github-release)
	$(call timed_make,"publish-everywhere: github release verification",_publish-everywhere-live-verify-github-release)
	$(call log_success,"Publish-everywhere completed")

_publish-execution-mode-check:
	@case "$(PUBLISH_EXECUTION_MODE)" in \
		local) \
			test "$${GITHUB_ACTIONS:-false}" != "true" || { printf "\033[1;31m[ERROR]\033[0m local publication is forbidden inside GitHub Actions\n" >&2; exit 2; }; \
			test "$(PYPI_TRUSTED_PUBLISHING)" = "never" || { printf "\033[1;31m[ERROR]\033[0m local publication requires PYPI_TRUSTED_PUBLISHING=never\n" >&2; exit 2; }; \
			test -z "$(NPM_PUBLISH_PROVENANCE_ARGS)" || { printf "\033[1;31m[ERROR]\033[0m local publication forbids NPM provenance\n" >&2; exit 2; }; \
			test "$(MCP_REGISTRY_LOGIN_METHOD)" = "github" || { printf "\033[1;31m[ERROR]\033[0m local publication requires MCP_REGISTRY_LOGIN_METHOD=github\n" >&2; exit 2; } \
			;; \
		ci) \
			test "$${GITHUB_ACTIONS:-false}" = "true" || { printf "\033[1;31m[ERROR]\033[0m ci publication requires GitHub Actions\n" >&2; exit 2; }; \
			test "$(PYPI_TRUSTED_PUBLISHING)" = "always" || { printf "\033[1;31m[ERROR]\033[0m ci publication requires PYPI_TRUSTED_PUBLISHING=always\n" >&2; exit 2; }; \
			test "$(NPM_PUBLISH_PROVENANCE_ARGS)" = "--provenance" || { printf "\033[1;31m[ERROR]\033[0m ci publication requires NPM provenance\n" >&2; exit 2; }; \
			test "$(MCP_REGISTRY_LOGIN_METHOD)" = "github-oidc" || { printf "\033[1;31m[ERROR]\033[0m ci publication requires MCP_REGISTRY_LOGIN_METHOD=github-oidc\n" >&2; exit 2; } \
			;; \
		*) \
			printf "\033[1;31m[ERROR]\033[0m PUBLISH_EXECUTION_MODE must be local or ci\n" >&2; \
			exit 2 \
			;; \
	esac

_publish-everywhere-preflight:
	@if [[ "$(PUBLISH_EVERYWHERE_SKIP_CHECKS)" == "1" ]]; then \
		printf "\033[1;32m[OK]\033[0m publish-everywhere: current local preflight evidence reused\n"; \
	else \
		$(MAKE) --no-print-directory publish-everywhere-check; \
	fi

_publish-everywhere-required-env-check:
	@test -n "$${DOCKERHUB_USERNAME:-}" || { printf "\033[1;31m[ERROR]\033[0m DOCKERHUB_USERNAME is required before publish-everywhere starts\n" >&2; exit 2; }
	@test -n "$${DOCKERHUB_TOKEN:-}" || { printf "\033[1;31m[ERROR]\033[0m DOCKERHUB_TOKEN is required before publish-everywhere starts\n" >&2; exit 2; }
	@if [[ "$(PUBLISH_EXECUTION_MODE)" == "local" ]]; then \
		test -n "$${UV_PUBLISH_TOKEN:-}" || { printf "\033[1;31m[ERROR]\033[0m UV_PUBLISH_TOKEN is required for local PyPI publication\n" >&2; exit 2; }; \
	else \
		test -n "$${HOMEBREW_TAP_TOKEN:-}" || { printf "\033[1;31m[ERROR]\033[0m HOMEBREW_TAP_TOKEN is required for CI publication\n" >&2; exit 2; }; \
		test -n "$${NODE_AUTH_TOKEN:-}" || { printf "\033[1;31m[ERROR]\033[0m NODE_AUTH_TOKEN is required for CI publication\n" >&2; exit 2; }; \
		test -n "$${GH_TOKEN:-$${GITHUB_TOKEN:-}}" || { printf "\033[1;31m[ERROR]\033[0m GH_TOKEN or GITHUB_TOKEN is required for CI publication\n" >&2; exit 2; }; \
	fi
	$(call log_success,"Publish-everywhere required environment is present")

_publish-everywhere-auth-preflight:
	@command -v gh >/dev/null 2>&1 || { printf "\033[1;31m[ERROR]\033[0m gh is required before publish-everywhere starts\n" >&2; exit 2; }
	@command -v mcp-publisher >/dev/null 2>&1 || { printf "\033[1;31m[ERROR]\033[0m mcp-publisher is required before publish-everywhere starts\n" >&2; exit 2; }
	@cd "$(NPM_DIR)" && $(NPM) whoami --registry "$(NPM_REGISTRY_URL)" >/dev/null
	@gh auth status --hostname "$(GITHUB_HOST)" >/dev/null 2>&1
	@visibility="$$(gh api --hostname "$(GITHUB_HOST)" "orgs/$(PUBLIC_NAMESPACE)/packages/container/$(DOCKER_IMAGE_NAME)" --jq '.visibility')" || { printf "\033[1;31m[ERROR]\033[0m GHCR package visibility check failed; token needs package read access\n" >&2; exit 2; }; \
		if [[ "$$visibility" != "public" ]]; then \
			printf "\033[1;31m[ERROR]\033[0m GHCR package is not public: %s/%s visibility=%s\n" "$(PUBLIC_NAMESPACE)" "$(DOCKER_IMAGE_NAME)" "$$visibility" >&2; \
			exit 2; \
		fi
	@HOMEBREW_EFFECTIVE_TOKEN="$${HOMEBREW_TAP_TOKEN:-}"; \
		if [[ -z "$$HOMEBREW_EFFECTIVE_TOKEN" ]]; then HOMEBREW_EFFECTIVE_TOKEN="$$(gh auth token --hostname "$(GITHUB_HOST)")"; fi; \
		export HOMEBREW_EFFECTIVE_TOKEN; \
		tmpdir="$$(mktemp -d)"; \
		trap 'rm -rf "$$tmpdir"' EXIT; \
		printf '%s\n' \
			'#!/usr/bin/env bash' \
			'case "$$1" in' \
			'  *Username*) printf "%s\n" x-access-token ;;' \
			'  *Password*) printf "%s\n" "$${HOMEBREW_EFFECTIVE_TOKEN}" ;;' \
			'esac' > "$$tmpdir/git-askpass.sh"; \
		chmod 0700 "$$tmpdir/git-askpass.sh"; \
		GIT_ASKPASS="$$tmpdir/git-askpass.sh" GIT_TERMINAL_PROMPT=0 git ls-remote "$(HOMEBREW_TAP_CLONE_URL)" HEAD >/dev/null
	@tmpdir="$$(mktemp -d)"; \
		trap 'rm -rf "$$tmpdir"' EXIT; \
		cp "$(ROOT)/registry/server.json" "$$tmpdir/server.json"; \
		registry_github_token="$$(gh auth token --hostname "$(GITHUB_HOST)")" || { printf "\033[1;31m[ERROR]\033[0m gh auth token failed for MCP Registry preflight\n" >&2; exit 2; }; \
		test -n "$$registry_github_token" || { printf "\033[1;31m[ERROR]\033[0m gh auth token returned empty for MCP Registry preflight\n" >&2; exit 2; }; \
		(cd "$$tmpdir" && MCP_GITHUB_TOKEN="$$registry_github_token" mcp-publisher login "$(MCP_REGISTRY_LOGIN_METHOD)" >/dev/null)
	$(call log_success,"Publish-everywhere auth preflight passed")

_publish-everywhere-docker-hub-public:
	$(call timed_make,"publish-everywhere: docker hub public ensure",docker-hub-public-ensure)

_publish-everywhere-registry-fanout:
	@$(PYTHON_BIN) "$(ROOT)/scripts/release_fanout.py" \
		--ledger "$(RELEASE_TRANSACTION_LEDGER)" \
		--jobs "$(PUBLISH_EVERYWHERE_JOBS)" \
		--step "npm::$(MAKE) --no-print-directory _publish-everywhere-npm" \
		--step "docker::$(MAKE) --no-print-directory _publish-everywhere-docker" \
		--step "mcp-registry::$(MAKE) --no-print-directory _publish-everywhere-mcp-registry" \
		--step "homebrew::$(MAKE) --no-print-directory _publish-everywhere-homebrew"
	$(call log_success,"Publish-everywhere registry fanout recorded: $(RELEASE_TRANSACTION_LEDGER)")

_publish-everywhere-live-verify-registries:
	@$(PYTHON_BIN) "$(ROOT)/scripts/verify_public_release.py" \
		--version "$(PACKAGE_VERSION)" \
		--checks pypi,npm,docker-hub,docker-registry,ghcr,mcp-registry,homebrew \
		--npm-package "$(NPM_PACKAGE_NAME)" \
		--docker-namespace "$(DOCKER_NAMESPACE)" \
		--docker-image-name "$(DOCKER_IMAGE_NAME)" \
		--mcp-registry-name "$(MCP_REGISTRY_NAME)" \
		--github-release-url "$(GITHUB_RELEASE_URL)" \
		--pypi-version-url "$(PYPI_VERSION_URL)" \
		--npm-registry-url "$(NPM_REGISTRY_URL)" \
		--docker-hub-api-repository-base-url "$(DOCKER_HUB_API_REPOSITORY_BASE_URL)" \
		--docker-registry-service "$(DOCKER_REGISTRY_SERVICE)" \
		--docker-registry-host "$(DOCKER_REGISTRY_HOST)" \
		--docker-registry-auth-url "$(DOCKER_REGISTRY_AUTH_URL)" \
		--docker-registry-manifest-base-url "$(DOCKER_REGISTRY_MANIFEST_BASE_URL)" \
		--ghcr-registry-service "$(GHCR_REGISTRY_SERVICE)" \
		--ghcr-registry-host "$(GHCR_REGISTRY_HOST)" \
		--ghcr-registry-auth-url "$(GHCR_REGISTRY_AUTH_URL)" \
		--ghcr-registry-manifest-base-url "$(GHCR_REGISTRY_MANIFEST_BASE_URL)" \
		--mcp-registry-search-url "$(MCP_REGISTRY_SEARCH_URL)" \
		--homebrew-formula-url "$(HOMEBREW_FORMULA_RAW_URL)"

_publish-everywhere-github-release:
	@command -v gh >/dev/null 2>&1 || { printf "\033[1;31m[ERROR]\033[0m gh is required to create the GitHub Release\n" >&2; exit 2; }
	@target="$$(git -C "$(ROOT)" rev-parse HEAD)"; \
	if gh release view "$(GITHUB_RELEASE_TAG)" --repo "$(GITHUB_REPO)" >/dev/null 2>&1; then \
		gh release edit "$(GITHUB_RELEASE_TAG)" \
			--repo "$(GITHUB_REPO)" \
			--target "$$target" \
			--title "$(GITHUB_RELEASE_TITLE)" \
			--notes-file "$(GITHUB_RELEASE_NOTES_FILE)" \
			--latest; \
	else \
		gh release create "$(GITHUB_RELEASE_TAG)" \
			--repo "$(GITHUB_REPO)" \
			--target "$$target" \
			--title "$(GITHUB_RELEASE_TITLE)" \
			--notes-file "$(GITHUB_RELEASE_NOTES_FILE)" \
			--latest; \
	fi
	$(call log_success,"GitHub Release published: $(GITHUB_RELEASE_TAG)")

_publish-everywhere-live-verify-github-release:
	@$(PYTHON_BIN) "$(ROOT)/scripts/verify_public_release.py" \
		--version "$(PACKAGE_VERSION)" \
		--checks github-release \
		--npm-package "$(NPM_PACKAGE_NAME)" \
		--docker-namespace "$(DOCKER_NAMESPACE)" \
		--docker-image-name "$(DOCKER_IMAGE_NAME)" \
		--mcp-registry-name "$(MCP_REGISTRY_NAME)" \
		--github-release-url "$(GITHUB_RELEASE_URL)" \
		--pypi-version-url "$(PYPI_VERSION_URL)" \
		--npm-registry-url "$(NPM_REGISTRY_URL)" \
		--docker-hub-api-repository-base-url "$(DOCKER_HUB_API_REPOSITORY_BASE_URL)" \
		--docker-registry-service "$(DOCKER_REGISTRY_SERVICE)" \
		--docker-registry-host "$(DOCKER_REGISTRY_HOST)" \
		--docker-registry-auth-url "$(DOCKER_REGISTRY_AUTH_URL)" \
		--docker-registry-manifest-base-url "$(DOCKER_REGISTRY_MANIFEST_BASE_URL)" \
		--ghcr-registry-service "$(GHCR_REGISTRY_SERVICE)" \
		--ghcr-registry-host "$(GHCR_REGISTRY_HOST)" \
		--ghcr-registry-auth-url "$(GHCR_REGISTRY_AUTH_URL)" \
		--ghcr-registry-manifest-base-url "$(GHCR_REGISTRY_MANIFEST_BASE_URL)" \
		--mcp-registry-search-url "$(MCP_REGISTRY_SEARCH_URL)" \
		--homebrew-formula-url "$(HOMEBREW_FORMULA_RAW_URL)"

_publish-everywhere-pypi:
	@action="$$("$(PYTHON_BIN)" "$(ROOT)/scripts/release_idempotency.py" \
		--surface pypi \
		--version "$(PACKAGE_VERSION)" \
		--dist-dir "$(PACKAGE_DIST_DIR)" \
		--pypi-version-url "$(PYPI_VERSION_URL)")" || { \
			printf "\033[1;31m[ERROR]\033[0m PyPI idempotency check failed (digest mismatch or metadata error)\n" >&2; \
			exit 2; \
		}; \
	if [ "$$action" = "skip" ]; then \
		printf "\033[1;32m[OK]\033[0m PyPI package already verified: %s==%s\n" "$(PYPI_PROJECT_NAME)" "$(PACKAGE_VERSION)"; \
	elif [ "$$action" = "publish" ]; then \
		command -v "$(UV)" >/dev/null 2>&1 || { printf "\033[1;31m[ERROR]\033[0m uv is required for publish-everywhere\n" >&2; exit 2; }; \
		"$(UV)" publish --trusted-publishing "$(PYPI_TRUSTED_PUBLISHING)" --check-url "$(PYPI_SIMPLE_CHECK_URL)" "$(PACKAGE_DIST_DIR)"/*; \
	else \
		printf "\033[1;31m[ERROR]\033[0m PyPI idempotency check returned invalid action: %s\n" "$$action" >&2; \
		exit 2; \
	fi
	$(call log_success,"PyPI publish target completed: $(PACKAGE_VERSION)")

_publish-everywhere-npm:
	@action="$$("$(PYTHON_BIN)" "$(ROOT)/scripts/release_idempotency.py" \
		--surface npm \
		--version "$(PACKAGE_VERSION)" \
		--package-dir "$(NPM_DIR)" \
		--package-name "$(NPM_PACKAGE_NAME)" \
		--npm-registry-url "$(NPM_REGISTRY_URL)" \
		--npm-command "$(NPM)")" || { \
			printf "\033[1;31m[ERROR]\033[0m NPM idempotency check failed (digest mismatch or metadata error)\n" >&2; \
			exit 2; \
		}; \
	if [ "$$action" = "skip" ]; then \
		printf "\033[1;32m[OK]\033[0m NPM package already verified: %s@%s\n" "$(NPM_PACKAGE_NAME)" "$(PACKAGE_VERSION)"; \
	elif [ "$$action" = "publish" ]; then \
		cd "$(NPM_DIR)" && $(NPM) publish --access public $(NPM_PUBLISH_PROVENANCE_ARGS); \
	else \
		printf "\033[1;31m[ERROR]\033[0m NPM idempotency check returned invalid action: %s\n" "$$action" >&2; \
		exit 2; \
	fi
	$(call log_success,"NPM publish target completed: $(NPM_PACKAGE_NAME)")

_publish-everywhere-docker-auth:
	@test -n "$${DOCKERHUB_USERNAME:-}" || { printf "\033[1;31m[ERROR]\033[0m DOCKERHUB_USERNAME is required for Docker registry login\n" >&2; exit 2; }
	@test -n "$${DOCKERHUB_TOKEN:-}" || { printf "\033[1;31m[ERROR]\033[0m DOCKERHUB_TOKEN is required for Docker registry login\n" >&2; exit 2; }
	@printf '%s' "$${DOCKERHUB_TOKEN}" | docker login "$(DOCKER_REGISTRY_HOST)" \
		--username "$${DOCKERHUB_USERNAME}" --password-stdin >/dev/null
	@ghcr_token="$$(gh auth token --hostname "$(GITHUB_HOST)")" || { printf "\033[1;31m[ERROR]\033[0m gh auth token failed for GHCR login\n" >&2; exit 2; }; \
		ghcr_username="$$(gh api --hostname "$(GITHUB_HOST)" user --jq .login)" || { printf "\033[1;31m[ERROR]\033[0m GitHub username lookup failed for GHCR login\n" >&2; exit 2; }; \
		test -n "$$ghcr_token" || { printf "\033[1;31m[ERROR]\033[0m gh auth token returned empty for GHCR login\n" >&2; exit 2; }; \
		test -n "$$ghcr_username" || { printf "\033[1;31m[ERROR]\033[0m GitHub username lookup returned empty for GHCR login\n" >&2; exit 2; }; \
		printf '%s' "$$ghcr_token" | docker login "$(GHCR_REGISTRY_HOST)" \
			--username "$$ghcr_username" --password-stdin >/dev/null
	$(call log_success,"Docker registry authentication passed")

_publish-everywhere-docker:
	@$(MAKE) --no-print-directory _publish-everywhere-docker-auth
	@$(MAKE) --no-print-directory _docker-release-builder-ensure
	@TAG_ARGS=(); \
	for image in $(DOCKER_PUBLISH_IMAGES); do TAG_ARGS+=("-t" "$$image"); done; \
	docker buildx build \
		--builder "$(DOCKER_RELEASE_BUILDER)" \
		--platform "$(DOCKER_PLATFORMS)" \
		--build-arg VERSION="$(PACKAGE_VERSION)" \
		--build-arg VCS_REF="$$(git -C "$(ROOT)" rev-parse --short HEAD 2>/dev/null || printf unknown)" \
		--build-arg SOURCE_URL="$(DOCKER_SOURCE_URL)" \
		--build-arg AUTHORS="$(PACKAGE_AUTHOR)" \
		--sbom=$(DOCKER_SBOM) \
		--provenance=$(DOCKER_PROVENANCE) \
		"$${TAG_ARGS[@]}" \
		--push \
		"$(ROOT)"
	$(call log_success,"Published Docker images: $(DOCKER_PUBLISH_IMAGES)")
	$(call timed_make,"publish child: docker-publish-check",docker-publish-check)

_publish-everywhere-mcp-registry:
	@action="$$("$(PYTHON_BIN)" "$(ROOT)/scripts/release_idempotency.py" \
		--surface mcp-registry \
		--version "$(PACKAGE_VERSION)" \
		--mcp-registry-name "$(MCP_REGISTRY_NAME)" \
		--mcp-registry-search-url "$(MCP_REGISTRY_SEARCH_URL)")" || { \
			printf "\033[1;31m[ERROR]\033[0m MCP Registry idempotency check failed (digest mismatch or metadata error)\n" >&2; \
			exit 2; \
		}; \
	if [ "$$action" = "skip" ]; then \
		printf "\033[1;32m[OK]\033[0m MCP Registry metadata already verified: %s %s\n" "$(MCP_REGISTRY_NAME)" "$(PACKAGE_VERSION)"; \
	elif [ "$$action" = "publish" ]; then \
		command -v mcp-publisher >/dev/null 2>&1 || { printf "\033[1;31m[ERROR]\033[0m mcp-publisher is required for publish-everywhere\n" >&2; exit 2; }; \
		tmpdir="$$(mktemp -d)"; \
		trap 'rm -rf "$$tmpdir"' EXIT; \
		cp "$(ROOT)/registry/server.json" "$$tmpdir/server.json"; \
		if [[ "$(PUBLISH_EXECUTION_MODE)" = "local" ]]; then \
			registry_github_token="$$(gh auth token --hostname "$(GITHUB_HOST)")" || { printf "\033[1;31m[ERROR]\033[0m gh auth token failed for MCP Registry publish\n" >&2; exit 2; }; \
			test -n "$$registry_github_token" || { printf "\033[1;31m[ERROR]\033[0m gh auth token returned empty for MCP Registry publish\n" >&2; exit 2; }; \
			(cd "$$tmpdir" && MCP_GITHUB_TOKEN="$$registry_github_token" mcp-publisher login "$(MCP_REGISTRY_LOGIN_METHOD)" && MCP_GITHUB_TOKEN="$$registry_github_token" mcp-publisher publish); \
		else \
			(cd "$$tmpdir" && mcp-publisher login "$(MCP_REGISTRY_LOGIN_METHOD)" && mcp-publisher publish); \
		fi; \
	else \
		printf "\033[1;31m[ERROR]\033[0m MCP Registry idempotency check returned invalid action: %s\n" "$$action" >&2; \
		exit 2; \
	fi
	$(call log_success,"MCP Registry publish target completed")

_publish-everywhere-homebrew:
	@HOMEBREW_EFFECTIVE_TOKEN="$${HOMEBREW_TAP_TOKEN:-}"; \
		if [[ -z "$$HOMEBREW_EFFECTIVE_TOKEN" ]]; then HOMEBREW_EFFECTIVE_TOKEN="$$(gh auth token --hostname "$(GITHUB_HOST)")"; fi; \
		export HOMEBREW_EFFECTIVE_TOKEN; \
		tmpdir="$$(mktemp -d)"; \
		trap 'rm -rf "$$tmpdir"' EXIT; \
		printf '%s\n' \
			'#!/usr/bin/env bash' \
			'case "$$1" in' \
			'  *Username*) printf "%s\n" x-access-token ;;' \
			'  *Password*) printf "%s\n" "$${HOMEBREW_EFFECTIVE_TOKEN}" ;;' \
			'  *) printf "\n" ;;' \
			'esac' > "$$tmpdir/git-askpass.sh"; \
		chmod 700 "$$tmpdir/git-askpass.sh"; \
		GIT_ASKPASS="$$tmpdir/git-askpass.sh" GIT_TERMINAL_PROMPT=0 \
			git clone --depth 1 --branch "$(HOMEBREW_TAP_BRANCH)" \
			"$(HOMEBREW_TAP_CLONE_URL)" "$$tmpdir/tap"; \
		"$(PYTHON)" "$(ROOT)/scripts/update_homebrew_formula.py" \
			--formula "$$tmpdir/tap/$(HOMEBREW_FORMULA_PATH)" \
			--version "$(PACKAGE_VERSION)" \
			--pypi-version-url "$(PYPI_VERSION_URL)" \
			--pypi-attempts "$(HOMEBREW_PYPI_ATTEMPTS)" \
			--pypi-retry-delay-seconds "$(HOMEBREW_PYPI_RETRY_DELAY_SECONDS)"; \
		if git -C "$$tmpdir/tap" diff --quiet -- "$(HOMEBREW_FORMULA_PATH)"; then \
			printf "\033[1;32m[OK]\033[0m Homebrew formula already current: %s %s\n" "$(HOMEBREW_TAP_REPO)" "$(PACKAGE_VERSION)"; \
		else \
			git -C "$$tmpdir/tap" config user.name "$(HOMEBREW_GIT_AUTHOR_NAME)"; \
			git -C "$$tmpdir/tap" config user.email "$(HOMEBREW_GIT_AUTHOR_EMAIL)"; \
			git -C "$$tmpdir/tap" add "$(HOMEBREW_FORMULA_PATH)"; \
			git -C "$$tmpdir/tap" commit -m "Update mcp-broker formula to $(PACKAGE_VERSION)"; \
			GIT_ASKPASS="$$tmpdir/git-askpass.sh" GIT_TERMINAL_PROMPT=0 git -C "$$tmpdir/tap" \
				push origin "HEAD:$(HOMEBREW_TAP_BRANCH)"; \
		fi
	$(call log_success,"Homebrew publish target completed: $(HOMEBREW_TAP_REPO)")
