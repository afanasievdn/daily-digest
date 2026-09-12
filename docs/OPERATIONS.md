# Эксплуатация и перенос кода

## Рабочая установка

Проект работает на VPS в `/opt/daily-digest`; контейнер в предыдущих проверках назывался `daily-digest`. Последний показанный запуск выполнен через `docker run`, с образом `daily-digest:2`, `--restart unless-stopped`, `.env` и bind mount `data:/data`. Наличие `compose.yaml` не доказывает, что текущий контейнер управляется Compose.

Привязка к GitHub **не останавливает и не пересобирает контейнер**. Актуальные `app.py`, Dockerfile, Compose и requirements уже перенесены с VPS в репозиторий без изменений. Перезапуск — отдельная операция.

## Привязка VPS к GitHub

### Текущее подключение

Актуальный код уже находится в GitHub. История передаётся на VPS через Git bundle, созданный из проверенного клона на Mac: это позволяет подключить рабочую папку без копирования приватных ключей на сервер. Ветка — `main`, upstream — `origin/main`; remote указывает на `ssh://git@ssh.github.com:443/afanasievdn/daily-digest.git`.

На VPS ещё нет отдельного ключа доступа к приватному GitHub-репозиторию. До его настройки прямые `git fetch`, `pull` и `push` на VPS не работают. Код уже сохранён в GitHub, а будущие изменения можно готовить через подключённый GitHub или авторизованный клон на Mac. Привязка Git сама по себе не предоставляет серверу доступ к аккаунту GitHub.

Обычный SSH-порт GitHub 22 недоступен с этого VPS; порт 443 отвечает. Для будущего подключения используйте [официальный вариант SSH через HTTPS-порт](https://docs.github.com/en/authentication/troubleshooting-ssh/using-ssh-over-the-https-port). Публичный host key GitHub проверяется отдельно от пользовательского ключа; проверка подлинности сервера не отключается.

На сервере предыдущий `.env.example`, если он существовал, сохраняется как игнорируемая защищённая резервная копия перед установкой нового шаблона. Рабочий `.env` и база не заменяются.

Проверка текущего состояния:

```bash
cd /opt/daily-digest
git status --short
git log -1 --oneline
git remote -v
git ls-files
```

В Git отслеживаются 11 файлов: четыре исходника, README, `.gitignore`, `.dockerignore`, `.env.example` и три документа в `docs/`. Неотслеживаемый `configure.py` остаётся локально; его нужно отдельно проверить перед возможным переносом.

### Первоначальный импорт — сохранённая инструкция

Следующая последовательность описывает первоначальный импорт в репозиторий, содержащий только документацию. После завершённого переноса **повторять её не нужно**. Проверка шага 5 ожидает четыре новых исходника и остановится, если они уже совпадают с GitHub. При повторной привязке сначала сравните существующее состояние и сохраните локальные файлы.

Выполняйте команды в Terminal **после SSH-входа на VPS**, под владельцем `/opt/daily-digest` (в существующей установке — root). Не выполняйте эти команды в локальном `/opt` на Mac или в среде запуска Python. Каждый блок выполняйте по порядку; при любой ошибке остановитесь и разберите её перед следующим блоком.

### 1. Доступ к приватному репозиторию

Нужны Git, SSH-клиент и доступ на запись к `afanasievdn/daily-digest`. Если Git отсутствует, установите его средствами вашей ОС.

Для VPS можно использовать отдельный SSH deploy key, ограниченный этим репозиторием. По [документации GitHub](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys) deploy key по умолчанию допускает только чтение; для первого push нужна галочка **Allow write access**.

Если подходящий SSH-доступ уже настроен, новый ключ не нужен. Иначе создайте отдельный ключ, не перезаписывая существующий:

```bash
install -d -m 700 "$HOME/.ssh"
if [ -e "$HOME/.ssh/daily_digest_github" ] || [ -e "$HOME/.ssh/daily_digest_github.pub" ]; then
  echo 'Ключ уже существует. Не перезаписывайте его.'
else
  ssh-keygen -t ed25519 -f "$HOME/.ssh/daily_digest_github" -C daily-digest-vps
fi
```

Добавьте содержимое **публичного** файла `~/.ssh/daily_digest_github.pub` в GitHub → репозиторий → Settings → Deploy keys → Add deploy key. Приватный файл без `.pub` остаётся на сервере. При создании ключа с парольной фразой загрузите его в SSH-agent перед работой.

Чтобы показать только публичный ключ:

```bash
cat "$HOME/.ssh/daily_digest_github.pub"
```

При первом SSH-подключении сверьте отпечаток сервера с [официальными отпечатками GitHub](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints). Не отключайте проверку host key. Проверка чтения:

```bash
GIT_SSH_COMMAND="ssh -i $HOME/.ssh/daily_digest_github -o IdentitiesOnly=yes" \
  git ls-remote git@github.com:afanasievdn/daily-digest.git refs/heads/main
```

Должна появиться строка с SHA и `refs/heads/main`: документация уже создала начальную историю. Если используете другой действующий SSH-доступ, выполните `git ls-remote` без `GIT_SSH_COMMAND`.

Не вставляйте токен GitHub в URL remote. При использовании HTTPS вводите учётные данные через подходящий credential helper, а не в строке команды.

### 2. Проверка исходной папки

Откройте отдельную Bash-сессию для следующих блоков. `set -e` прерывает её при ошибках; после этого не продолжайте вслепую.

```bash
bash
set -e
cd /opt/daily-digest
for file in app.py Dockerfile compose.yaml requirements.txt; do
  test -f "$file"
  test ! -L "$file"
done
if git rev-parse --show-toplevel >/dev/null 2>&1; then
  echo 'Папка уже находится в Git-репозитории. Остановитесь: нужна проверка существующей истории и remote.'
  exit 1
fi
if [ -L docs ]; then
  echo 'docs является символической ссылкой. Остановитесь и проверьте папку.'
  exit 1
fi
for file in README.md .gitignore docs/ARCHITECTURE.md docs/OPERATIONS.md docs/CHANGELOG.md; do
  if [ -e "$file" ] || [ -L "$file" ]; then
    echo "Уже существует $file. Остановитесь и сохраните/сравните его до установки документации."
    exit 1
  fi
done
```

Не переименовывайте и не удаляйте `.env` или `data/`; не меняйте владельца данных ради Git. Проверьте четыре исходных файла локально на сервере: токены, пароли и персональные настройки должны читаться из окружения, а не быть строками в `app.py`, Dockerfile или Compose. Ссылки `env_file: .env` и `./data:/data` допустимы — они не добавляют содержимое файлов в Git.

### 3. Защитная копия исходных файлов

Копия только четырёх исходных файлов сохраняется вне репозитория, с ограниченным доступом. Она не включает `.env` или базу.

```bash
digest_backup_dir=$(mktemp -d /opt/daily-digest-code-backup.XXXXXX)
chmod 700 "$digest_backup_dir"
cp -p app.py Dockerfile compose.yaml requirements.txt "$digest_backup_dir/"
```

Базу отдельно резервируют способом из раздела ниже, если планируют менять приложение. Для одного подключения Git остановка бота и копирование базы не нужны.

### 4. Подключение существующей истории GitHub

```bash
git init -b main
git remote add origin git@github.com:afanasievdn/daily-digest.git
```

Если используете отдельный ключ из шага 1, настройте его только для этого репозитория:

```bash
git config core.sshCommand "ssh -i $HOME/.ssh/daily_digest_github -o IdentitiesOnly=yes"
```

Затем:

```bash
git fetch origin
git rev-parse --verify origin/main
git reset --mixed origin/main
git branch --set-upstream-to=origin/main main
git restore --source=origin/main --worktree -- \
  README.md .gitignore docs/ARCHITECTURE.md docs/OPERATIONS.md docs/CHANGELOG.md
```

`reset --mixed` здесь подключает текущую ветку к существующему коммиту и обновляет индекс, **не заменяя рабочие файлы**. `restore` записывает только пять перечисленных файлов документации, отсутствие которых проверено ранее. Код, `.env` и `data/` сохраняются. Не заменяйте эти команды на `reset --hard`, не используйте `git clean`, `git add .` или push с `--force`.

### 5. Проверка исключений и подготовка ровно четырёх файлов

```bash
git check-ignore --no-index -- \
  .env .env.local .env.backup data/digest.sqlite3 \
  digest.sqlite3 digest.sqlite3-wal digest.sqlite3-shm app.py.backup-test
git status --short --ignored
git add -- app.py Dockerfile compose.yaml requirements.txt
git diff --cached --name-only
git diff --cached --check
```

Первая команда должна перечислить все восемь проверяемых путей. `.env.example` намеренно разрешён, но существующий серверный шаблон, `configure.py` и любые другие файлы этим `git add` не добавляются.

Следующая проверка ограничивает состав будущего коммита и отслеживаемых файлов. Она не читает `.env` или базу:

```bash
python3 - <<'PY'
import subprocess

def paths(*args):
    out = subprocess.check_output(['git', *args])
    return set(filter(None, out.decode().split('\0')))

code = {'app.py', 'Dockerfile', 'compose.yaml', 'requirements.txt'}
docs = {'README.md', '.gitignore', 'docs/ARCHITECTURE.md',
        'docs/OPERATIONS.md', 'docs/CHANGELOG.md'}
staged = paths('diff', '--cached', '--name-only', '-z')
tracked = paths('ls-files', '-z')
if staged != code or tracked != code | docs:
    raise SystemExit('STOP: состав файлов отличается от ожидаемых четырёх исходников и пяти файлов документации.')
for path in ['.env', '.env.local', '.env.backup', 'data/digest.sqlite3',
             'digest.sqlite3', 'digest.sqlite3-wal', 'digest.sqlite3-shm',
             'app.py.backup-test']:
    result = subprocess.run(['git', 'check-ignore', '--no-index', '-q', '--', path])
    if result.returncode != 0:
        raise SystemExit('STOP: один из путей с секретами/данными не исключён.')
print('OK: в Git только ожидаемые файлы; окружение, данные и копии исключены.')
PY
```

### 6. Проверка содержимого перед коммитом

Автоматическая проверка ниже ищет характерные токены и приватные ключи **в версии файлов из индекса**, не выводя найденные значения. Она дополняет ручной просмотр, но не обнаруживает любые возможные пароли.

```bash
python3 - <<'PY'
import re
import subprocess

patterns = [
    rb'\b[0-9]{5,16}:[A-Za-z0-9_-]{30,}\b',
    rb'\bgh[pousr]_[A-Za-z0-9_]{20,}\b',
    rb'\bgithub_pat_[A-Za-z0-9_]{20,}\b',
    rb'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----',
    rb'https?://[^\s/@]+:[^\s/@]+@',
]
flagged = []
for path in ['app.py', 'Dockerfile', 'compose.yaml', 'requirements.txt']:
    content = subprocess.check_output(['git', 'show', ':' + path])
    if any(re.search(pattern, content) for pattern in patterns):
        flagged.append(path)
if flagged:
    raise SystemExit('STOP: возможный секрет в ' + ', '.join(flagged) + '. Не отправляйте коммит.')
print('Характерные токены/ключи не найдены. Проверьте diff вручную на сервере.')
PY
git diff --cached -- app.py Dockerfile compose.yaml requirements.txt
```

Просмотрите diff в своём терминале. Не отправляйте его в чат, если обнаружите секрет. Если есть реальные значения, сначала замените их чтением из окружения и снова выполните `git add` для исправленного файла, проверку состава и проверку содержимого. Изменение рабочего кода требует отдельной проверки перед пересборкой контейнера.

### 7. Коммит и обычный push

Проверьте автора коммита:

```bash
git config --get user.name || true
git config --get user.email || true
```

Если имя или email не настроены, задайте их через `git config user.name` и `git config user.email` только для этого репозитория. Можно использовать свой GitHub noreply email; вымышленные данные в команды не подставляются.

После успешных проверок:

```bash
git commit -m "Import current Daily Digest application from VPS"
git push -u origin main
git status --short
git ls-files
```

`git ls-files` должен показать только девять ожидаемых файлов. Откройте GitHub и убедитесь, что четыре исходника появились, а `.env` и `data/` отсутствуют. Наличие локальных неотслеживаемых `configure.py` и `.env.example` допустимо. Если push отклонён из-за новых коммитов в GitHub, остановитесь, получите и сравните изменения; не применяйте `--force`.

Если соединение оборвалось, сначала проверьте текущую стадию через `git status` и `git remote -v`. Не запускайте всю последовательность заново в уже созданном репозитории.

## Конфигурация и `.env.example`

Существующий `.env` остаётся на VPS с правами доступа только для владельца. Не выводите его в общие логи. По обсуждению подтверждены токен бота, ID владельца, зона `Europe/Moscow`, время 20:30, окно 24 часа и `DATA_DIR=/data` при запуске контейнера.

Ключи проверены по актуальному `app.py`:

| Ключ окружения | Значение / назначение |
| --- | --- |
| `BOT_TOKEN` | Обязательный токен бота, только в локальном `.env` |
| `OWNER_CHAT_ID` | Обязательный числовой ID личного чата владельца |
| `TIMEZONE` | `Europe/Moscow` |
| `DIGEST_HOUR` | `20` |
| `DIGEST_MINUTE` | `30` |
| `LOOKBACK_HOURS` | `24` |
| `DATA_DIR` | `/data` внутри контейнера |

Новый `.env.example` создан вручную по этим ключам: токен и ID пустые. Серверный `.env` не копировался. Для новой установки скопируйте шаблон в `.env` только если `.env` ещё не существует, ограничьте доступ и заполните секреты локально. На существующей установке оставляйте рабочий `.env` без изменений.

## Docker и дальнейшее обновление

Сначала определите, как запущен текущий контейнер:

```bash
docker ps --filter name=daily-digest
docker inspect daily-digest --format '{{json .Mounts}}'
docker inspect daily-digest --format '{{index .Config.Labels "com.docker.compose.project"}}'
```

Не используйте полный `docker inspect` для публикации вывода: он может показать окружение с токеном. В Compose должны быть корректные `env_file`, bind mount данных, политика перезапуска и часовой пояс приложения. Часовой пояс ОС сам по себе не задаёт расписание JobQueue.

Git-исключения не действуют на Docker build context. В репозитории добавлен `.dockerignore`. Перед следующей сборкой проверьте, что он исключает `.git`, `.env`, `.env.*`, `data`, `*.sqlite*`, `*.db*`, резервные копии и журналы. В текущем Dockerfile код и зависимости копируются явно; будущий `COPY . .` без исключений может включить данные в образ. См. [Docker build context](https://docs.docker.com/build/concepts/context/#dockerignore-files).

Код копируется внутрь образа: после изменения `app.py` простой restart не обновляет приложение. Для подтверждённой установки под Compose обычный цикл — `git pull --ff-only`, проверка изменений, затем `docker compose up -d --build`. Применяйте его только после проверки, что Compose действительно управляет этой установкой. Для существующего запуска через `docker run` используйте проверенную процедуру пересоздания с теми же параметрами или отдельно спланируйте миграцию на Compose. Не запускайте второй экземпляр поверх первого и не используйте `docker compose down -v`.

Перед обновлением проверьте, что рабочая папка не содержит незакоммиченных изменений. Сохраните защищённую резервную копию базы. После обновления проверьте состояние контейнера, `/status` (20:30, Europe/Moscow, 24 часа), ручной `/digest` и ближайший плановый выпуск. Ручной выпуск не проверяет автоматическую очистку.

## Диагностика

```bash
docker logs --tail=100 daily-digest
```

Журналы смотрите на сервере: сообщения об ошибках HTTP могут содержать URL с токеном. Перед отправкой третьим лицам удалите токены и персональные значения. При проблемах проверьте доступность веб-страниц каналов, сообщения retry, регистрацию JobQueue, момент очистки и фактический результат в Telegram.

При сетевых сбоях выпуск может быть частичным. Нельзя считать «нет публикаций» и «не удалось прочитать канал» одинаковым результатом. После простоя больше 48 часов может потребоваться ручная очистка старых сообщений; после простоя больше 24 часов прежние посты не попадут в скользящее окно.

## Резервная копия SQLite

Не копируйте одну активную SQLite-базу обычным `cp`: при работающем боте используйте SQLite backup API. Пример создаёт защищённый каталог вне репозитория и не выводит содержимое базы:

```bash
python3 - <<'PY'
import os
import sqlite3
import tempfile
from pathlib import Path

os.umask(0o077)
source = Path('/opt/daily-digest/data/digest.sqlite3')
if not source.is_file():
    raise SystemExit('База не найдена; проверьте путь.')
folder = Path(tempfile.mkdtemp(prefix='daily-digest-db-backup.', dir='/opt'))
with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src:
    with sqlite3.connect(folder / 'digest.sqlite3') as dst:
        src.backup(dst)
print('Защищённая копия сохранена:', folder)
PY
```

База содержит источники и служебные ID; резервные копии также остаются вне Git. Восстанавливайте данные при остановленном контейнере, сохраняя текущую базу и права владельца UID 10001, если именно он используется установкой.

## Если секрет уже попал в Git

`.gitignore` не влияет на уже отслеживаемые файлы: это прямо описано в [документации Git](https://git-scm.com/docs/gitignore). До push удалите секрет из индекса и всех локальных коммитов, которые планируется отправить. Если токен уже отправлен, сначала отзовите/замените его, затем отдельно очистите историю; простое удаление файла новым коммитом не убирает прежнюю копию. Не применяйте переписывание истории автоматически к рабочему репозиторию.
