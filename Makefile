# OpenRPC descriptions of the Model Context Protocol.
#
#   make            vendor the upstream schemas, generate, validate
#   make generate   regenerate spec/ from the already-vendored schemas
#   make validate   check the generated documents
#   make check      verify vendored schemas still match SOURCES.json, then validate
#   make clean      remove generated documents

PYTHON ?= python3

.PHONY: all vendor generate validate check clean

all: vendor generate validate

vendor:
	$(PYTHON) tools/vendor.py

generate:
	$(PYTHON) tools/gen_openrpc.py

validate:
	$(PYTHON) tools/validate.py

check:
	$(PYTHON) tools/vendor.py --check
	$(PYTHON) tools/validate.py

clean:
	rm -rf spec
