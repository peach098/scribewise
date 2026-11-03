# Публикация лендинга в S3

Лендинг — статические файлы из `landing/`, сборка не нужна. Их загружают в бакет, включают хостинг статического сайта и привязывают домен с HTTPS. Ниже два варианта:

- **Cloud.ru Object Storage + CDN** — для аудитории в РФ: серверы в России, оплата в рублях.
- **AWS S3 + CloudFront** — для зарубежной аудитории.

Оба варианта используют один и тот же скрипт `scripts/deploy-landing.sh` и AWS CLI: Object Storage в Cloud.ru совместим с S3 API.

## Что выкладывается

```
landing/
  index.html       → открывает /ru/ или /en/ по языку браузера
  ru/index.html
  en/index.html
  assets/          → стили, скрипт, favicon, картинки для превью ссылок
  robots.txt       → разрешает индексацию и указывает на sitemap.xml
  sitemap.xml      → /ru/ и /en/ с языковыми альтернативами
```

Корневой `index.html` помечен `noindex`: это только переадресация, в поиск попадают `/ru/` и `/en/`. Если появится новая страница, добавьте её в `sitemap.xml`.

Ссылки внутри страниц относительные, а `/ru/` и `/en/` — это папки. Поэтому сайт должен отдавать `index.html` по адресу папки. Website-эндпоинт AWS S3 делает это сам. В Cloud.ru это нужно проверить после первой выкладки (см. «Проверка после публикации»).

## Перед первой публикацией

1. **Ссылка на бету.** Кнопки ведут в бота `https://t.me/the_scribewise_bot`. Если адрес поменяется, замените `BETA_URL` в `landing/assets/main.js` и запасные `href` в `landing/ru/index.html` и `landing/en/index.html` — их видят посетители с выключенным JavaScript.
2. **Домен в метатегах.** Соцсети и поисковики требуют полных адресов, поэтому в `hreflang`, `og:url` и `og:image` всех трёх `index.html` прописан домен `https://scribewise.dev`. Если лендинг переедет на другой домен, замените его во всех трёх файлах.
3. **GitHub.** Когда репозиторий станет публичным, впишите адрес в `REPO_URL` в `main.js`, и в подвале появится ссылка.
4. **AWS CLI.** Установите [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html), он нужен в обоих вариантах.

## Вариант 1. Cloud.ru Object Storage + CDN

Сам бакет отдаёт сайт по адресу вида `https://<имя>.s3-website.cloud.ru`. Свой домен с HTTPS подключается через сервис CDN Cloud.ru.

### 1. Ключи доступа

1. В [личном кабинете Cloud.ru](https://console.cloud.ru) создайте сервисный аккаунт для выкладки и ключ доступа для него (Key ID и Key Secret). Подойдёт и персональный ключ, но отдельный аккаунт проще отозвать. Порядок действий: [получение ключа доступа к API Object Storage](https://cloud.ru/docs/s3e/ug/topics/api__api-key). Сохраните Key Secret: его показывают один раз.
2. Узнайте Tenant ID: **Хранение данных → Object Storage → Параметры работы с API**.
3. Настройте профиль AWS CLI. Идентификатор ключа в Cloud.ru пишется вместе с ID тенанта через двоеточие:

   ```bash
   aws configure --profile cloudru
   # AWS Access Key ID:     <tenant_id>:<key_id>
   # AWS Secret Access Key: <key_secret>
   # Default region name:   ru-central-1
   # Default output format: json
   ```

   Свежие версии AWS CLI v2 по умолчанию добавляют к загрузкам контрольные суммы, которые не все S3-совместимые хранилища принимают. Если загрузка падает с ошибкой про checksum, добавьте в `~/.aws/config` в секцию `[profile cloudru]`:

   ```ini
   request_checksum_calculation = when_required
   response_checksum_validation = when_required
   ```

### 2. Бакет

1. **Хранение данных → Object Storage → Создать бакет.** Задайте бакету **доменное имя**, например `scribewise`: без него не работают ни публичный доступ, ни хостинг. Имя должно совпадать с тем, что вы передадите скрипту в `BUCKET`.
2. **Публичный доступ.** В бакете откройте **Безопасность → Bucket Policy** и включите **Публичный доступ к бакету**. Он разрешает только чтение объектов. На загрузку и удаление по-прежнему нужны ключи.

### 3. Загрузка

```bash
AWS_PROFILE=cloudru BUCKET=scribewise ENDPOINT=https://s3.cloud.ru \
  ./scripts/deploy-landing.sh
```

Чтобы сначала посмотреть, что изменится, ничего не загружая, добавьте `DRY_RUN=1`.

Загружать нужно до включения хостинга: Cloud.ru просит, чтобы главная страница и страница ошибки уже лежали в бакете.

### 4. Хостинг сайта

В бакете откройте вкладку **Настройка хостинга сайта**:

- **Главная страница:** `index.html`;
- **Страница ошибки:** `index.html`. Отдельной 404-страницы у лендинга нет, а корневая страница отправит посетителя с несуществующего адреса на версию на его языке.

Сохраните. Сайт откроется по адресу `https://<доменное-имя-бакета>.s3-website.cloud.ru/`.

### 5. Свой домен и HTTPS через CDN

1. **CDN → SSL-сертификаты.** Закажите бесплатный сертификат Let's Encrypt на домен сайта или загрузите свой.
2. **CDN → HTTP кеширование → Создать ресурс:**
   - **Источник данных:** `<доменное-имя-бакета>.s3-website.cloud.ru`. Источником должен быть именно website-адрес: только он отдаёт `index.html` по адресам папок и страницу ошибки.
   - Включите **Использовать HTTPS при запросе к источникам**.
   - **Оптимизация:** «Статика».
   - **SSL-сертификат:** сертификат из шага 1.
   - **Желаемый CNAME:** домен сайта, например `www.scribewise.ru`.
3. У DNS-провайдера создайте CNAME-запись с домена сайта на технический адрес ресурса вида `xxxx.a.trbcdn.net`, который CDN покажет после сохранения. Изменения DNS расходятся до 72 часов.

CNAME нельзя поставить на корень домена (`scribewise.ru`). Поэтому сайт размещают на `www.scribewise.ru`, а с корня настраивают переадресацию на `www` у регистратора домена.

В документации CDN website-адрес бакета как источник не описан отдельно. Если ресурс отдаёт ошибки, уточните в поддержке Cloud.ru, какое значение указать в поле Hostname для такого источника.

После повторных выкладок очистите кеш CDN-ресурса в его настройках, чтобы изменения появились сразу.

Документация Cloud.ru: [AWS CLI](https://cloud.ru/docs/s3e/ug/topics/tools__aws-cli), [публичный доступ](https://cloud.ru/docs/s3e/ug/topics/guides__public-access-for-bucket), [хостинг сайта](https://cloud.ru/docs/s3e/ug/topics/guides__hosting__setup), [CDN-ресурс](https://cloud.ru/docs/cdn/ug/topics/http-cache__create-resource).

## Вариант 2. AWS S3 + CloudFront

S3 отдаёт сайт только по HTTP, поэтому HTTPS и домен даёт CloudFront.

### 1. Бакет с хостингом сайта

```bash
export AWS_PROFILE=default BUCKET=scribewise-landing REGION=eu-central-1

aws s3 mb "s3://$BUCKET" --region "$REGION"
aws s3 website "s3://$BUCKET" --index-document index.html

# Разрешить публичное чтение: снять блокировку и добавить политику.
aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=false,RestrictPublicBuckets=false
aws s3api put-bucket-policy --bucket "$BUCKET" --policy "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [{\"Effect\": \"Allow\", \"Principal\": \"*\",
    \"Action\": \"s3:GetObject\", \"Resource\": \"arn:aws:s3:::$BUCKET/*\"}]
}"
```

Сайт доступен по адресу `http://<бакет>.s3-website.<регион>.amazonaws.com`.

### 2. Загрузка

```bash
BUCKET=scribewise-landing ./scripts/deploy-landing.sh
```

### 3. CloudFront, домен и HTTPS

1. **ACM** в регионе **us-east-1** (CloudFront берёт сертификаты только оттуда): запросите сертификат на домен и подтвердите его DNS-записью.
2. **CloudFront** → **Create distribution**:
   - **Origin domain:** вставьте вручную адрес website-эндпоинта `<бакет>.s3-website.<регион>.amazonaws.com`, а не бакет из выпадающего списка. Только website-эндпоинт отдаёт `ru/index.html` по адресу `/ru/`.
   - **Protocol:** HTTP only (website-эндпоинт S3 не умеет HTTPS, шифрование будет между посетителем и CloudFront).
   - **Viewer protocol policy:** Redirect HTTP to HTTPS.
   - **Alternate domain name:** ваш домен; **Custom SSL certificate:** сертификат из шага 1.
   - **Cache policy:** `CachingOptimized`. Он учитывает заголовки `Cache-Control`, которые ставит скрипт.
3. У регистратора направьте домен на адрес дистрибутива `dxxxx.cloudfront.net`: CNAME для поддомена или ALIAS/ANAME для корня (в Route 53 — запись Alias).

После повторных выкладок сбросьте кеш CloudFront, чтобы изменения появились сразу:

```bash
aws cloudfront create-invalidation --distribution-id <ID> --paths "/*"
```

## Как работает скрипт

`scripts/deploy-landing.sh` делает два прохода `aws s3 sync`:

1. **Стили, скрипт, картинки** — кешируются браузером на сутки (`max-age=86400`). У файлов нет хешей в именах, поэтому больше суток ставить нельзя: правки дольше не дойдут до посетителей.
2. **HTML** — `no-cache`: браузер при каждом заходе сверяется с сервером, так что исправленный текст виден сразу.

Флаг `--delete` убирает из бакета файлы, которых больше нет в `landing/`.

## Проверка после публикации

- Откройте корень сайта: должна открыться версия на языке браузера. Переключатель RU | EN меняет версию.
- Кнопка «Занять место в бете» ведёт в нужный Telegram-чат.
- Проверьте превью ссылки, например через [opengraph.xyz](https://www.opengraph.xyz) или отправив ссылку себе в Telegram. Должна подтянуться картинка с заголовком.
- Убедитесь, что адрес `/ru/` без `index.html` открывается, а не отдаёт 403 или 404. В AWS ошибка означает, что источником указан не website-эндпоинт. В Cloud.ru, если website-адрес бакета всё равно не отдаёт `index.html` по адресам папок, переведите ссылки на явные адреса командой ниже и выложите заново:

  ```bash
  sed -i 's#href="\.\./en/"#href="../en/index.html"#; s#href="\.\./ru/"#href="../ru/index.html"#; s#href="\./"#href="./index.html"#' landing/ru/index.html landing/en/index.html
  sed -i 's#location.replace("/" + lang + "/")#location.replace("/" + lang + "/index.html")#; s#href="/\(ru\|en\)/"#href="/\1/index.html"#g' landing/index.html
  ```
