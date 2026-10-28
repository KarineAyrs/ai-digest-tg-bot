# AI Digest Bot

Telegram-бот, который собирает свежие материалы по AI из arXiv, Telegram-каналов и блогов, отбирает и разбирает их через Claude и присылает три отдельных дайджеста в личку. В канал дайджест попадает только после вашего подтверждения.

## Расписание

| День, 10:00 МСК | Раздел | Окно |
|---|---|---|
| Понедельник | 📡 Telegram-каналы | с прошлой плановой сводки (в первый раз 7 дней) |
| Среда | 📚 Статьи arXiv + HF Daily Papers | с прошлой плановой сводки (7 дней) |
| Пятница | ✍️ Блоги авторов | с прошлой плановой сводки (30 дней) |

Если бот был выключен в 10:00, плановая сводка запустится, когда он поднимется, но только до 13:00. Так деплой посреди дня не запускает неожиданный дайджест. Если новых материалов нет, плановая сводка ничего не присылает.

## Команды

- `/tg [дни]`, `/papers [дни]`, `/blogs [дни]` — раздел вручную, в любой момент (`/papers 14`)
- `/digest [дни]` — все три раздела подряд
- `/arxiv prompt injection` — поиск по arXiv
- `/pool` — подборка: что в ней, убрать пункты, опубликовать одним постом
- `/publish pool` + текст с новой строки — опубликовать подборку со вступлением
- `/publish papers 3` — сразу в канал отдельные пункты (`/publish tg 1 2`); без номеров — весь дайджест раздела
- `/status` — расписание и прошлые запуски
- `/help` — справка

### Публикация в канал

Ничего не публикуется само. Под каждым превью есть кнопки с номерами пунктов:
- `＋ 3` — добавить пункт 3 в **подборку**, `✓ 3` — он уже в подборке (нажатие убирает), `📢 3` — уже опубликован;
- «📢 Весь дайджест в канал» — опубликовать все ещё не опубликованные пункты этого превью.

Подборку можно копить всю неделю из разных разделов, а потом выпустить одним постом через `/pool` или `/publish pool`. В посте пункты сгруппированы по разделам, нумерация сквозная. Подборка хранит сами пункты, поэтому перезапуск разделов её не ломает. Опубликованные пункты бот помнит и второй раз в канал не отправит (повторно — `force`).

Вступление (необязательно) пишется после команды, удобнее с новой строки; форматирование из Telegram сохраняется:
```
/publish pool
Главное за неделю: много про безопасность агентов.
```

Плановые сводки показывают только то, что ещё не приходило (бот помнит это отдельно для каждого раздела). Ручные команды показывают весь указанный период, даже если что-то уже было, и тоже запоминают показанное, чтобы плановая сводка его не повторила.

## Как собирается каждый раздел

**📚 Статьи** — воронка от дешёвых фильтров к дорогим:
1. Все статьи за окно из категорий arXiv (обычно тысячи) + HF Daily Papers с апвоутами.
2. Бесплатный предфильтр по ключевым словам из `keywords.txt` (заголовок весит втрое больше аннотации) и бонус за апвоуты на HF. Остаётся до 400 статей.
3. **Haiku** по заголовкам выбирает ~30.
4. **Haiku** по аннотациям выбирает 6 и ставит значимость 1–5.
5. **Sonnet** читает полный текст этих 6 из HTML-версии `arxiv.org/html/<id>` и пишет разбор. Если HTML нет, остаётся разбор по аннотации: PDF бот не парсит.

Поля: ключевые идеи · почему это важно · артефакты (только если есть).

**📡 Telegram-каналы** — посты за окно (бот листает `t.me/s/канал` назад постранично, без Telegram API). **Haiku** склеивает одну новость из разных каналов в один пункт и выбирает 6. Поля: что произошло · почему это важно · первоисточник (только ссылка, которая есть в самих постах).

**✍️ Блоги** — посты из RSS/Atom за окно. **Haiku** выбирает до 4. Поля: главная мысль · аргументы и данные; плюс значимость 1–5 и время чтения, посчитанное по тексту.

Во всех разделах модель возвращает только номер материала и текст. Ссылки и источники бот подставляет из собранных данных, поэтому модель не может их выдумать.

## Источники

**Научные статьи**
- arXiv: cs.AI, cs.CL, cs.LG, cs.CR, cs.NE, stat.ML
- HuggingFace Daily Papers

**Блоги и рассылки (RSS/Atom)**
- [Lil'Log](https://lilianweng.github.io) — Lilian Weng, RL, safety, агенты
- [Interconnects](https://nathanlambert.ai) — Nathan Lambert, post-training и RLHF
- [Ahead of AI](https://magazine.sebastianraschka.com) — Sebastian Raschka, архитектуры
- [Import AI](https://jack-clark.net) — Jack Clark, индустрия и политика
- [Last Week in AI](https://lastweekin.ai) — агрегатор новостей
- [The Gradient](https://thegradient.pub) — академические разборы
- [Simon Willison](https://simonwillison.net) — агенты, prompt injection (только длинные посты)
- [Eugene Yan](https://eugeneyan.com) — applied ML, агенты

**Telegram-каналы**
- @seeallochnaya — индустрия и тренды (RU)
- @gonzo_ml — глубокие разборы статей (RU)
- @abstractDL — короткие саммари статей (RU)
- @ai_newz — новости и ресёрч (RU)
- @lovedeathtransformers — ML-инсайды (RU)
- @interconnects — Nathan Lambert (EN)
- @gradientdissent — Weights & Biases (EN)
- @ai_alignment_feed — safety и alignment (EN)
- @lmsys_org — бенчмарки и serving (EN)

## Установка

### Требования

- Python 3.10+
- VPS или любой сервер с постоянным интернетом
- Аккаунты: [Anthropic Console](https://console.anthropic.com), Telegram-бот через [@BotFather](https://t.me/BotFather)

### 0. Сервисный пользователь (один раз на сервер)

Бот работает не от root, а от общего непривилегированного пользователя `bots`, от которого запускаются все служебные сервисы. Без sudo, вход только по SSH-ключу.

```bash
# от root
adduser --disabled-password --gecos "" bots      # Debian/Ubuntu (или: useradd -m -s /bin/bash bots)
install -d -m 700 -o bots -g bots /home/bots/.ssh
cp ~/.ssh/authorized_keys /home/bots/.ssh/       # тот же ключ, что и для root
chown bots:bots /home/bots/.ssh/authorized_keys
chmod 600 /home/bots/.ssh/authorized_keys
```

Все шаги ниже, кроме шага 6, выполняются от `bots`: `sudo -iu bots` (или `ssh bots@<server>`).

### 1. Клонировать репозиторий

```bash
cd ~
git clone https://github.com/YOUR_USERNAME/digest-bot ai-digest
cd ai-digest
chmod 700 .
```

### 2. Установить зависимости (в venv)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

В каждой новой SSH-сессии перед ручным запуском снова выполните `source .venv/bin/activate`.

### 3. Настроить переменные окружения

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Заполнить три обязательных значения:

```
ANTHROPIC_API_KEY=sk-ant-...       # console.anthropic.com → API Keys
TELEGRAM_BOT_TOKEN=123456:ABC-...  # @BotFather → /newbot
TELEGRAM_CHAT_ID=123456789         # см. ниже
```

Необязательные:

```
TELEGRAM_CHANNEL_ID=@channel_name  # канал для /publish
DIGEST_MODEL=claude-haiku-5-5      # отбор и короткие разборы
DEEP_MODEL=claude-sonnet-5-5       # разбор полного текста статей arXiv
```

Без кавычек и без `export`. `.env` читается из папки проекта, откуда бы ни запускался бот.

**Как узнать TELEGRAM_CHAT_ID:** напишите боту `/start`, затем откройте в браузере:
```
https://api.telegram.org/bot<ВАШ_TOKEN>/getUpdates
```
Найдите `"id"` внутри `"chat"` — это и есть ваш chat_id.

**Публикация в канал:** добавьте бота в канал администратором с правом «Публикация сообщений» и укажите `TELEGRAM_CHANNEL_ID`. Для публичного канала это `@имя`, для приватного — число вида `-100…` (можно узнать, переслав пост из канала боту @userinfobot).

**Лимиты Anthropic:** разбор полных текстов отправляет до ~90K входных токенов за пару минут, поэтому лимит воркспейса для Sonnet должен быть не ниже ~150K input tokens/min. При ошибке 429 бот сам подождёт и повторит запрос.

### 4. Персонализация (необязательно)

```bash
cp system_prompt.example.txt system_prompt.txt
nano system_prompt.txt
```

`system_prompt.txt` в `.gitignore`. Если файла нет, используется обезличенный профиль из `prompts.py`.

### 5. Запустить и проверить

```bash
source .venv/bin/activate   # если ещё не активирован
python bot.py
```

Напишите боту `/help` — должен ответить. Ctrl+C для остановки.
Если не хватает ключей или не установлен `python-dotenv`, бот сразу завершится с понятной ошибкой.

### 6. Запуск как системный сервис (Linux, systemd)

Юнит уже настроен на пользователя `bots` и папку `/home/bots/ai-digest`.
systemd не умеет `activate`, поэтому в юните указан полный путь `.venv/bin/python`.

```bash
# от root
cp /home/bots/ai-digest/digest-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now digest-bot
systemctl status digest-bot   # должно быть active (running)
```

Логи:
```bash
journalctl -u digest-bot -f
```

После изменения кода (от root): `systemctl restart digest-bot`.

## Настройка

| Что | Где |
|---|---|
| Профиль читателя (что вам важно) | `system_prompt.txt` |
| Ключевые слова предфильтра статей | `keywords.txt` |
| Каналы, блоги, категории arXiv | начало `sources.py` |
| Число пунктов в разделах | `N_PAPERS`, `N_TG`, `N_BLOGS` в `digest.py` |
| Дни и время расписания | `SECTIONS`, `SCHEDULE_HOUR` в `bot.py` |
| Модели | `DIGEST_MODEL`, `DEEP_MODEL` в `.env` |
| Канал для публикации | `TELEGRAM_CHANNEL_ID` в `.env` |

## Безопасность

- Файл `.env` не попадает в git (добавлен в `.gitignore`)
- Бот отвечает **только** вашему `TELEGRAM_CHAT_ID`, остальные игнорируются
- Бот работает через polling — не открывает никаких входящих портов
- systemd-юнит с hardening: бот может писать только в свою папку, без повышения привилегий
- Рекомендуется отдельный workspace в Anthropic Console со своими spend и rate limits

## Стоимость

Отбор на Haiku: доли цента за дайджест. Разбор полных текстов на Sonnet (4–6 статей по ~15K токенов) — основная часть стоимости, порядка нескольких центов за дайджест. Точные цены — в Anthropic Console.

## Структура проекта

```
ai-digest/
├── bot.py                     # Telegram: команды, расписание, публикация
├── digest.py                  # три пайплайна + вызовы Claude
├── sources.py                 # сбор: arXiv, HF, t.me/s, RSS/Atom
├── prompts.py                 # промпты для всех этапов
├── keywords.txt               # предфильтр статей
├── system_prompt.example.txt  # шаблон личного профиля
├── system_prompt.txt          # ваш профиль (не в git)
├── requirements.txt
├── .env.example               # шаблон переменных окружения
├── .env                       # ваши ключи (не в git)
├── digest-bot.service         # systemd-юнит
├── state.json                 # прошлые запуски, присланное, превью (создаётся автоматически)
└── README.md
```
