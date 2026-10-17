# Developer entry points. Needs: git, Go >= 1.27 (for the Ledger), Python >= 3.9. Nothing here costs money.
LEDGER_REPO ?= https://github.com/formancehq/ledger.git
LEDGER_REF  ?= 9a4bb29c8670e6f8c1a166bb85d31d26107f18a2
BUILD       ?= $(CURDIR)/.build
LEDGER_BIN  ?= $(BUILD)/ledger-bin
LEDGERCTL   ?= $(BUILD)/ledgerctl
SANDBOX_FLAGS ?=

.PHONY: ledger test test-pg test-hardened lint audit alerts keys clean

ledger: $(LEDGER_BIN)
$(LEDGER_BIN):
	mkdir -p $(BUILD)
	test -d $(BUILD)/ledger-src || git clone $(LEDGER_REPO) $(BUILD)/ledger-src
	git -C $(BUILD)/ledger-src checkout --quiet $(LEDGER_REF)
	cd $(BUILD)/ledger-src && go build -o $(LEDGER_BIN) . && go build -o $(LEDGERCTL) ./cmd/ledgerctl

test: ledger
	LEDGER_BIN=$(LEDGER_BIN) LEDGERCTL=$(LEDGERCTL) LEDGER_EXTRA_FLAGS="$(SANDBOX_FLAGS)" treasury/run-local-tests.sh

test-pg: ledger
	@test -n "$(PG_URL)" || (echo "set PG_URL=postgresql://user:pw@host:5432/db" && exit 1)
	pip install -r treasury/requirements.txt
	PG_URL=$(PG_URL) LEDGER_BIN=$(LEDGER_BIN) LEDGERCTL=$(LEDGERCTL) LEDGER_EXTRA_FLAGS="$(SANDBOX_FLAGS)" treasury/run-local-tests.sh

test-hardened: ledger  ## engine as uid 10001 from a read-only tree (needs PG_URL and root for setpriv)
	HARDENED_RUN=1 PG_URL=$(PG_URL) LEDGER_BIN=$(LEDGER_BIN) LEDGERCTL=$(LEDGERCTL) LEDGER_EXTRA_FLAGS="$(SANDBOX_FLAGS)" treasury/run-local-tests.sh

lint:
	python3 -m pyflakes treasury gke/*.py scripts/*.py
	shellcheck -S warning gke/gke-deploy.sh treasury/run-local-tests.sh scripts/*.sh

audit:
	mkdir -p $(BUILD)
	gke/gke-deploy.sh --render-only --non-interactive --config gke/ci-answers.env --out $(BUILD)/render

alerts:  ## promtool lint + unit tests of the PrometheusRule (needs promtool)
	gke/test-alerts.sh

keys:  ## print a fresh TREASURY_API_KEYS value for deploy/docker-compose.yml
	@python3 -c "import json,secrets;print(json.dumps({k:{'secret':secrets.token_urlsafe(48),'scopes':s,'rps':200} for k,s in {'ops':['*']}.items()}))"

clean:
	rm -rf $(BUILD)
