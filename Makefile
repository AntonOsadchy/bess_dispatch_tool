COMPOSE ?= docker compose -f docker/docker-compose.yml
SERVICE ?= app

.PHONY: build shell run run-dispatch python pip-install clean

build:
	$(COMPOSE) build $(SERVICE)

shell:
	$(COMPOSE) run --rm $(SERVICE) bash

run:
	$(COMPOSE) run --rm $(SERVICE) python3 src/bess_dispatch_opt.py --spec specification.txt

run-dispatch: run

python:
	$(COMPOSE) run --rm $(SERVICE) python3 $(ARGS)

pip-install:
	$(COMPOSE) run --rm $(SERVICE) pip install -r docker/requirements.txt

clean:
	$(COMPOSE) down --rmi local 2>/dev/null || true
