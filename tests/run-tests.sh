#!/usr/bin/env bash
# run-tests.sh — runs every suite that does not need a database.
#
#   ./tests/run-tests.sh          both offline suites
#   ./tests/run-tests.sh py       Python only
#   ./tests/run-tests.sh js       JavaScript only
#
# The SQL suite is deliberately excluded: it creates and drops
# partitions and must not be pointed at production. Run it by hand
# against a scratch database — see tests/sql/test_schema.sql.
#
# Nothing here touches Supabase, Telegram, or the network. No secrets
# are needed, which is what makes it safe to run in CI.

set -euo pipefail
cd "$(dirname "$0")/.."

WHICH="${1:-all}"
rc=0

run_py() {
    echo "=== Python ==="
    if ! command -v pytest >/dev/null 2>&1; then
        echo "pytest not found: pip install pytest openpyxl requests python-dotenv" >&2
        return 1
    fi
    python -m pytest tests/ -q "${@:2}" || rc=1
}

run_js() {
    echo
    echo "=== JavaScript ==="
    if ! command -v node >/dev/null 2>&1; then
        echo "node not found (18+ required for the built-in test runner)" >&2
        return 1
    fi
    # The scanner suite needs the __test seam appended to
    # dashboard/strategy-scanner.js — see tests/patches/.
    if ! grep -q "__test" dashboard/strategy-scanner.js; then
        echo "note: scanner test seam not applied; skipping scanner suite"
        node --test "tests/js/strategy-engine.test.mjs" || rc=1
    else
        node --test "tests/js/*.test.mjs" || rc=1
    fi
}

case "$WHICH" in
    py) run_py "$@" ;;
    js) run_js ;;
    all) run_py "$@"; run_js ;;
    *) echo "usage: $0 [py|js|all]" >&2; exit 2 ;;
esac

echo
if [ "$rc" -eq 0 ]; then
    echo "OK — xfail/todo entries are known defects, see tests/TESTPLAN.md"
else
    echo "FAILURES above"
fi
exit "$rc"
