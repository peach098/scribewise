#!/usr/bin/env bash
# Выкладывает лендинг из landing/ в S3-бакет (AWS S3 или Cloud.ru Object Storage).
#
#   BUCKET=scribewise.app ./scripts/deploy-landing.sh
#   BUCKET=scribewise ENDPOINT=https://s3.cloud.ru ./scripts/deploy-landing.sh
#   DRY_RUN=1 BUCKET=... ./scripts/deploy-landing.sh   # показать, что изменится, ничего не загружая
#
# Инструкция целиком: docs/deploy-s3.md
set -euo pipefail

: "${BUCKET:?Укажите бакет: BUCKET=имя-бакета}"
SRC="$(cd "$(dirname "$0")/../landing" && pwd)"

args=()
[[ -n "${ENDPOINT:-}" ]] && args+=(--endpoint-url "$ENDPOINT")
[[ -n "${DRY_RUN:-}" ]] && args+=(--dryrun)

# 1. Всё, кроме HTML: кешируем на сутки. Имена файлов без хешей, поэтому дольше не ставим.
#    --delete убирает из бакета файлы, удалённые из landing/ (HTML этот шаг не трогает).
aws s3 sync "$SRC" "s3://$BUCKET" ${args[@]+"${args[@]}"} \
  --delete \
  --exclude "*.html" --exclude ".*" \
  --cache-control "public, max-age=86400"

# 2. HTML: браузер каждый раз сверяется с сервером, чтобы правки текста были видны сразу.
aws s3 sync "$SRC" "s3://$BUCKET" ${args[@]+"${args[@]}"} \
  --delete \
  --exclude "*" --include "*.html" \
  --content-type "text/html; charset=utf-8" \
  --cache-control "no-cache"

echo "Готово: s3://$BUCKET"
