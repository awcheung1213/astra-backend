.PHONY: help
help:
	clear
	@echo -------------------------------
	@echo - Makefile Target Information -
	@echo -------------------------------
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'


.PHONY: start
start: ## start the project (api :8000 + worker + postgres :5432; migrations + queue schema on boot)
	clear
	@echo Starting the project
	@echo -------------------------------
	docker compose up -d


.PHONY: purge
purge: ## Stop the whole stack and DELETE ALL DATA (containers + db volume)
	clear
	@echo "Purging: all containers + database volume"
	@echo -------------------------------
	docker compose down -v
	@echo "Done. Restart with 'make start' (needs .env)."


.PHONY: shell
shell: ## Shell into the evergreen-backend container
	clear
	@echo Connecting to container
	@echo -------------------------------
	docker compose run --rm evergreen-backend /bin/bash --login


.PHONY: tinker
tinker: ## Atomic e2e run (tinker.py). Stops the live worker so tinker is the ONLY executor, restarts it after. args="--live --steps weekly ..."
	clear
	@echo Running tinker.py
	@echo -------------------------------
	docker compose stop evergreen-backend-worker
	docker compose run --rm --no-deps evergreen-backend python tinker.py $(args); status=$$?; docker compose start evergreen-backend-worker; exit $$status


.PHONY: check
check: ## Lint, Type Check, and Test
	docker compose up -d evergreen-backend-database
	clear
	@echo -------------------------------
	@echo Linting
	@echo -------------------------------
	docker compose run --rm --no-deps evergreen-backend flake8 . --show-source --statistic

	@echo -------------------------------
	@echo Type Checking
	@echo -------------------------------
	docker compose run --rm --no-deps evergreen-backend mypy .

	@echo -------------------------------
	@echo Formatting
	@echo -------------------------------
	docker compose run --rm --no-deps evergreen-backend black --check --diff .
	docker compose run --rm --no-deps evergreen-backend isort --check-only --profile black .

	@echo -------------------------------
	@echo Testing
	@echo -------------------------------
	docker compose run --rm --no-deps evergreen-backend pytest --disable-pytest-warnings -vv -p no:cacheprovider --show-capture=no


.PHONY: lint
lint: ## Lint
	clear
	@echo -------------------------------
	@echo Linting
	@echo -------------------------------
	docker compose run --rm evergreen-backend flake8 . --show-source --statistic


.PHONY: type
type: ## Type Check
	clear
	@echo -------------------------------
	@echo Type Checking
	@echo -------------------------------
	docker compose run --rm evergreen-backend mypy .


.PHONY: format
format: ## Format
	clear
	@echo -------------------------------
	@echo Formatting
	@echo -------------------------------
	docker compose run --rm evergreen-backend black .
	docker compose run --rm evergreen-backend isort . --profile black


.PHONY: test
test: ## Test k=filter_expression
	clear
	@echo -------------------------------
	@echo Testing
	@echo -------------------------------
	docker compose run --rm --no-deps evergreen-backend pytest --disable-pytest-warnings -vv -s -p no:cacheprovider --show-capture=no $(if $(k),-k "$(k)")


.PHONY: migration
migration: ## Create database migration
	clear
	@echo Creating database migration
	@echo -------------------------------
	docker compose run --rm evergreen-backend alembic revision --autogenerate -m "$(filter-out $@,$(MAKECMDGOALS))"


.PHONY: upgrade
upgrade: ## Upgrade to latest database migration
	clear
	@echo Upgrading the database
	@echo -------------------------------
	docker compose run --rm evergreen-backend alembic upgrade head


.PHONY: downgrade
downgrade: ## Downgrade the database
	clear
	@echo Downgrading the database
	@echo -------------------------------
	docker compose run --rm evergreen-backend alembic downgrade -1


.PHONY: queue-schema
queue-schema: ## Apply procrastinate's own schema (once per database; also runs on boot)
	clear
	@echo Applying the queue schema
	@echo -------------------------------
	docker compose run --rm evergreen-backend procrastinate --app=worker.app.app schema --apply


%: ## ignore all commands that are not defined
	@:
