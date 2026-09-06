strip_quotes = $(subst ",,$(1))

define log
	@printf "\033[1;34m[INFO]\033[0m %s\n" "$(call strip_quotes,$(1))"
endef

define log_success
	@printf "\033[1;32m[OK]\033[0m %s\n" "$(call strip_quotes,$(1))"
endef

define log_error
	printf "\033[1;31m[ERROR]\033[0m %s\n" "$(call strip_quotes,$(1))" >&2
endef

define log_step
	@printf "\n\033[1;36m=== %s ===\033[0m\n" "$(call strip_quotes,$(1))"
endef

define timed_make
	+@label="$(call strip_quotes,$(1))"; \
	log="$(call strip_quotes,$(3))"; \
	start_epoch=$$(date +%s); \
	start_time=$$(date +"%Y-%m-%d %H:%M:%S %Z"); \
	printf "\033[1;35m[TIME]\033[0m start %s at %s\n" "$$label" "$$start_time"; \
	if [[ -n "$$log" ]]; then \
		rm -f "$$log"; \
		if $(MAKE) --no-print-directory $(2) >"$$log" 2>&1; then status=0; else status=$$?; fi; \
	else \
		if $(MAKE) --no-print-directory $(2); then status=0; else status=$$?; fi; \
	fi; \
	end_epoch=$$(date +%s); \
	end_time=$$(date +"%Y-%m-%d %H:%M:%S %Z"); \
	elapsed_seconds=$$((end_epoch - start_epoch)); \
	elapsed_human=$$(printf "%02d:%02d:%02d" $$((elapsed_seconds / 3600)) $$(((elapsed_seconds % 3600) / 60)) $$((elapsed_seconds % 60))); \
	if [[ $$status -eq 0 ]]; then \
		printf "\033[1;35m[TIME]\033[0m end %s at %s elapsed=%s elapsed_seconds=%s status=%s\n" "$$label" "$$end_time" "$$elapsed_human" "$$elapsed_seconds" "$$status"; \
	else \
		printf "\033[1;35m[TIME]\033[0m end %s at %s elapsed=%s elapsed_seconds=%s status=%s\n" "$$label" "$$end_time" "$$elapsed_human" "$$elapsed_seconds" "$$status" >&2; \
		if [[ -n "$$log" ]]; then tail -n 80 "$$log" >&2; fi; \
	fi; \
	exit $$status
endef
