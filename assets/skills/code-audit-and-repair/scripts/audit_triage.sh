#!/usr/bin/env bash
# Одноразовая статическая сортировка аудитируемого репозитория. Запускай до чтения кода.
# Использование: bash audit_triage.sh [repo_dir]
# Код выхода всегда 0, если сам скрипт может работать — находки печатаются, не фатальны.

set -uo pipefail
REPO="${1:-.}"
cd "$REPO" || { echo "cannot enter $REPO"; exit 2; }

echo "=== repo: $(pwd) ==="
echo
echo "=== git state ==="
git rev-parse --short HEAD 2>/dev/null || echo "(not a git repo)"
git status --porcelain 2>/dev/null | head -20

echo
echo "=== 1. компилируется ли всё? ==="
python -m compileall -q . -x '(\.venv|__pycache__|\.git|node_modules)' 2>&1 | grep -v '^Listing' | head -30
if [ "${PIPESTATUS[0]:-0}" -eq 0 ]; then echo "all files compile"; fi

echo
echo "=== 2. неопределённые имена (реальные баги, гарантированные краши) ==="
python -m ruff check . --select F821 --output-format concise 2>&1 | tail -30

echo
echo "=== 3. сводка линта класса ошибок ==="
python -m ruff check . --select F,E9 --statistics 2>&1 | tail -25

echo
echo "=== 4. рискованный паттерн broad-except-returns-empty ==="
grep -rn -B2 -A2 'except.*:$' --include='*.py' . 2>/dev/null \
  | grep -E 'return \{\}|return \[\]|return None|pass$' | head -20 \
  || echo "(эвристика не нашла — проверь чтением функций load/read)"

echo
echo "=== 5. платформенные верхнеуровневые импорты ==="
grep -rn -E '^(import|from) (fcntl|pwd|grp|termios|resource)\b' --include='*.py' . 2>/dev/null | head -20 \
  || echo "(нет POSIX-only верхнеуровневых импортов)"

echo
echo "=== 6. захардкоженные интерпретаторы / POSIX venv-пути ==="
grep -rn -E '\.venv/(bin|Scripts)|/usr/bin/python3' --include='*.py' . 2>/dev/null | head -15 \
  || echo "(нет)"

echo
echo "=== 7. секреты, которые должны быть в gitignore ==="
git ls-files 2>/dev/null | grep -Ei '(^|/)(keys?\.json|\.env|.*_session\.json|.*_state\.json|credentials.*|.*\.pem)' \
  && echo "!! трекнутые файлы, похожие на секреты — добавь в .gitignore" \
  || echo "нет трекнутых файлов, похожих на секреты"

echo
echo "=== 8. тесты (зелёные тесты НЕ доказывают корректность) ==="
python -m pytest tests -q 2>&1 | tail -8 || echo "(нет tests/ или pytest недоступен)"

echo
echo "=== сортировка завершена. Дальше: гоняй реальные точки входа. ==="
