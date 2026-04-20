# `just` prints bash comments in stdout by default. this suppresses that
set ignore-comments := true

check-lint: check-format

format:
    uv run ruff check --fix-only .
    uv run shed

check-format:
    uv run ruff check .
    uv run shed
    git diff --exit-code

check-typing:
    uv run mypy src/

check-tests:
    PYTHONWARNDEFAULTENCODING=1 uv run pytest tests

check-tests-coverage-normal:
    PYTHONWARNDEFAULTENCODING=1 uv run coverage run --data-file=.coverage.normal -m pytest tests
    uv run coverage combine --data-file=.coverage.normal

check-tests-coverage-antithesis:
    ANTITHESIS_OUTPUT_DIR="$HOME/antithesis-output" PYTHONWARNDEFAULTENCODING=1 uv run coverage run --data-file=.coverage.antithesis -m pytest tests
    uv run coverage combine --data-file=.coverage.antithesis

check-coverage-report:
    uv run coverage combine .coverage.normal .coverage.antithesis
    uv run coverage report

check-coverage: check-tests-coverage-normal check-tests-coverage-antithesis check-coverage-report

# these aliases are provided as ux improvements for local developers. CI should use the longer
# forms.
test: check-tests
lint: check-lint
typecheck: check-typing
coverage: check-coverage
check: check-lint check-typing check-tests
