# comfy-mcp — MCP-инструменты генерации и редактирования изображений через ComfyUI

Три MCP-сервера для LM Studio, работающие с локальным или удалённым ComfyUI.
Адрес ComfyUI — **переменная установки**: `./installer/install.sh http://<хост-ComfyUI>:8188`
(macOS/Linux) или `.\installer\install.ps1 http://<хост-ComfyUI>:8188` (Windows).

| Сервер | Инструменты | Назначение |
|---|---|---|
| `comfy-image-gen` | `generate_image`, `check_image` | генерация картинки с нуля (nvidia workflow) |
| `comfy-image-edit` | `edit_image`, `check_image`, `list_images`, `convert_image` (+ `save_images` только при env `COMFY_ENABLE_SAVE_IMAGES=1`) | редактирование картинки по текстовой инструкции (qwen-image-edit) + конвертация формата/качества |
| `comfy-image-upscale` | `upscale_image`, `check_image`, `list_images` | нейро-апскейл SeedVR2 |

Ключевая особенность протокола: `edit_image` и `generate_image` **не ждут готовый результат** — они ставят задачу в очередь ComfyUI и сразу возвращают `job_id` («задача взята в работу»). Готовность проверяется периодическими вызовами `check_image`, который возвращает готовую картинку инлайн. Так инструмент не упирается в таймаут tool-вызова (рендеринг занимает 1–5 минут).

English version: [README.md](README.md)

## Мониторинг: ошибки и прогресс

- **Общий реестр задач.** Все три сервера дублируют статусы задач в `~/.comfy_mcp_jobs.json`. `check_image` любого сервера принимает любой `job_id` — даже если задачу создал другой сервер или сервер был перезапущен (для завершённых задач картинка дочитывается с диска). Записи старше суток вычищаются автоматически.
- **Ошибка/отмена задачи.** Poller на каждом опросе проверяет `status.status_str` в `/history/<prompt_id>`: если задача упала (например, битая картинка в `LoadImage`) или была отменена, `check_image` сразу вернёт описание ошибки — узел, его тип и текст исключения (например, `LoadImage (node 470): cannot identify image file ...`), без ожидания таймаута.
- **Процент готовности.** Сервер слушает WebSocket ComfyUI (`/ws`) и получает события `progress` (шаги сэмплера). Требуется пакет `websocket-client` (ставится инсталлером). Прогресс виден в ответах `check_image`, пока задача рендерится: `Progress: 10/25 steps (40%)`. Важно: ComfyUI шлёт события прогресса **только клиенту, отправившему задачу** (по `client_id`), поэтому прогресс виден только для задач, запущенных самим MCP-сервером. Пока задача стоит в очереди за чужими задачами, прогресс появится только когда дойдёт до её выполнения.

## Как устроен приём картинок из чата

LM Studio складывает вложения чата в `~/.lmstudio/user-files/` (Windows: `%USERPROFILE%\.lmstudio\user-files\`). Внутри `comfy-image-edit` работает фоновый watch-поток: новые картинки-вложения автоматически копируются в папку inbox (под исходным именем, с префиксом `chat_`), после чего видны через `list_images`. При первом запуске исторические файлы библиотеки не копируются — только новые вложения.

Папки:
- `~/comfy_edit_inbox` (Windows: `%USERPROFILE%\comfy_edit_inbox`) — входящие картинки для редактирования/апскейла (авто + ручные), общая для edit- и upscale-серверов;
- `~/comfy_images` (Windows: `%USERPROFILE%\comfy_images`) — сохранённые результаты генерации, редактирования и апскейла.

## Конвертация формата и качества

`convert_image(paths, format, quality, max_width, max_height)` в `comfy-image-edit` конвертирует картинки в JPG/PNG/WEBP (качество 1–100, default 90), опционально уменьшая так, чтобы ширина ≤ `max_width` и/или высота ≤ `max_height` (пропорции сохраняются, апскейла нет — идеально для фотографий с телефона), сохраняя результат рядом с исходником (для файлов из inbox — прямо в inbox). Типовой сценарий: рендер идёт заметно быстрее с уменьшенным JPG-исходником, поэтому перед `edit_image` большие фото стоит сжать:

1. `list_images()` → пути;
2. `convert_image(paths=[...], format="jpg", quality=85, max_width=1920)` → пути конвертированных файлов;
3. `edit_image(..., source_image=<jpg-путь>)`.

JPEG не поддерживает прозрачность — альфа кладётся на белый фон.

## Апскейл (comfy-image-upscale)

`upscale_image(source_image, multiplier | longer_size, scale_method)` — нейро-апскейл SeedVR2 7B через ComfyUI. Масштаб задаётся либо `multiplier` (0.01–8, default 2), либо `longer_size` (целевая длинная сторона в пикселях, соотношение сохраняется) — второй вариант точнее для «довести до 2048px». `scale_method` (default `lanczos`) — интерполяция пред-ресайза внутри пайплайна. Результат — PNG в `~/comfy_images` (сжимать апскейл lossy-конверсией внутри workflow бессмысленно; при необходимости потом примените `convert_image`). Тот же двухфазный протокол: `job_id` → опросы `check_image`; апскейл тяжёлый — рендер может занять заметно дольше редактирования.

## Файлы workflow

Три `*.json`-файла в корне репозитория **обязательны** — каждый сервер загружает
свой workflow при отправке задачи и посылает его в ComfyUI вместе с каждым запросом.
Это обычные API-экспорты workflow ComfyUI, в них **нет локальных данных** (ни путей,
ни адресов, ни информации о машине — только id узлов, параметры и имена моделей из
таблицы ниже). Скачивать что-либо в ComfyUI для их использования не нужно — они
отправляются через API, а не устанавливаются. Если используете другие модели —
экспортируйте свой workflow в API-формате и замените соответствующий файл
(сохраните id узлов, зашитые в константах в шапке сервера — см. таблицу требований ниже).

## Требования к ComfyUI

Файлы `*.json` в этом репозитории — **API-экспорты workflow ComfyUI**: они
отправляются в ComfyUI как есть. Поэтому на целевом ComfyUI должны быть
установлены те же модели и кастомные ноды, иначе задачи будут падать с ошибками
выполнения (текст ошибки возвращается через `check_image`, так что отсутствие
модели легко диагностировать).

| Файл workflow | Модели (в папках `models/` ComfyUI) | Кастомные ноды |
|---|---|---|
| `nvidia-image-gen.json` | `z_image_turbo_bf16.safetensors` (diffusion models), `qwen_3_4b.safetensors` (text encoders), `ae.safetensors` (vae) | только core-ноды |
| `image_qwen_image_2_1_image_edit.json` | `qwen_image_2.1_int8_convrot.safetensors`, `qwen3vl_8b_int8_convrot.safetensors`, `qwen_image_2.1_vae_bf16.safetensors`, `qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors` | `SaveImageAdvanced`, `TextEncodeQwenImage21`, `QwenImage21Cache`, `ComfySwitchNode`, `BatchImagesNode`, `PrimitiveStringMultiline`, `TextGenerate`, `PreviewAny` |
| `utility_seedvr2_7b_int8_upscale_image.json` | `seedvr2_7b_int8_convrot.safetensors`, `seedvr2_ema_vae_fp16.safetensors` | кастомные ноды SeedVR2 (`SeedVR2Preprocess`, `SeedVR2Conditioning`, `SeedVR2PostProcessing`), `ResizeImageMaskNode`, `JoinImageWithAlpha`, `VAEEncodeTiled`, `VAEDecodeTiled` |

Если используете другие модели/workflows — экспортируйте свой workflow в API-формате
(ComfyUI: «Workflow → Export (API)») и замените соответствующий `.json`-файл.
Серверы ссылаются на ноды по id, поэтому сохраните id узлов, зашитые в константах
в шапке сервера (id save-ноды и текстовых/размерных входов), либо обновите их.

## Установка

### Выбор адреса ComfyUI (важно!)

- **LM Studio и ComfyUI на одном компьютере** → используйте `http://localhost:8188`
  (или `http://127.0.0.1:8188`). Дефолтного режима прослушивания ComfyUI достаточно.
- **Два компьютера в локальной сети** → используйте `http://<IP-компьютера-ComfyUI>:8188` и:
  1. запускайте ComfyUI с флагом `--listen 0.0.0.0` (по умолчанию он слушает только
     `127.0.0.1` и недоступен с других машин);
  2. **закрепите за обоими компьютерами фиксированные IP в роутере** (DHCP reservation).
     Если IP компьютера с ComfyUI изменится, сервера молча потеряют цель, пока вы не
     перезапустите инсталлер с новым URL.

### macOS / Linux

Требования: Python ≥ 3.10 (`brew install python`), LM Studio.

1. Склонировать/скопировать весь репозиторий (серверы `.py`, workflow-файлы `.json`, папку `installer`).
2. Запустить:
   ```bash
   cd comfy-mcp/installer
   ./install.sh http://<хост-ComfyUI>:8188
   ```
   (без аргументов возьмёт дефолтный URL; если подходящий Python не найден, скрипт
   напечатает точные команды `brew install python@3.14`).
3. Перезапустить LM Studio (или Developer → MCP → Restart). Появятся инструменты
   `comfy-image-gen`, `comfy-image-edit` и `comfy-image-upscale`.
4. Вставить системный промт (ниже) в системный промт модели / project rules в LM Studio.

Скрипт делает: venv + `pip install "mcp<2" websocket-client Pillow`, подстановку URL
ComfyUI во все серверы, создание рабочих папок, регистрацию серверов в
`~/.lmstudio/mcp.json` (merge с бэкапом `.bak`, старые записи
`comfy_mcp_server`/`edit_mcp_server` заменяет на новые имена).

### Windows

Требования: Python ≥ 3.10 (установщик с python.org, галочка «Add python.exe to PATH»,
или `winget install Python.Python.3.14`), LM Studio.

1. Склонировать/скопировать репозиторий.
2. В PowerShell:
   ```powershell
   cd comfy-mcp\installer
   .\install.ps1 http://<хост-ComfyUI>:8188
   ```
3. Перезапустить LM Studio (Developer → MCP → Restart).
4. Вставить системный промт (ниже).

PowerShell-инсталлер выполняет те же шаги, что и bash-версия, но с путями Windows:
venv в `.venv\Scripts\python.exe`, папки inbox/результатов под `%USERPROFILE%`,
конфиг в `%USERPROFILE%\.lmstudio\mcp.json`.

### Обновление

Адрес ComfyUI живёт в `local.env` (untracked, пишет инсталлер) — трекаемые файлы
всегда совпадают с origin, поэтому обновления проходят чисто и без конфликтов:

```bash
cd comfy-mcp/installer
./update.sh --check    # что нового (коммиты + changelog), ничего не меняет
./update.sh --yes      # обновить: fetch, reset --hard origin/main, синк venv,
                       # остановка протухших MCP-процессов, проверки здоровья
```

- Windows: `.\update.ps1 -Check` / `.\update.ps1 -Yes`.
- Настройки переживают обновление: `local.env`, `VERSION`, `.lmstudio_seen.json`
  — untracked. НЕ редактируйте трекаемые файлы в установке — держите её чистой.
- Релизы помечены тегами (`v1.0`, …); чтобы закрепить машину на конкретной
  версии — `git checkout <тег>` вместо запуска апдейтера.
- `comfy_status` показывает версию установки (`Install: <коммит>`) — удобно
  проверять, что на какой машине крутится.

### Удаление

```bash
# macOS / Linux
cd comfy-mcp/installer && ./uninstall.sh
```
```powershell
# Windows
cd comfy-mcp\installer
.\uninstall.ps1
```
Убирает серверы из `~/.lmstudio/mcp.json` (конфиг бэкапится в `.bak`). Файлы проекта и venv не трогает.

## Использование с другими MCP-клиентами

Сервера — стандартные **stdio MCP-серверы**, в них нет ничего специфичного для
LM Studio. Любой MCP-клиент может их запустить: укажите клиенту
`<repo>/.venv/bin/python` (macOS/Linux) или `<repo>\.venv\Scripts\python.exe`
(Windows) со скриптом сервера в аргументах. Инсталлер лишь автоматизирует
создание venv и регистрацию ниже.

| Клиент | Регистрация | Примечания |
|---|---|---|
| LM Studio | `~/.lmstudio/mcp.json` (делает инсталлер) | включён watch-поток автоприёма вложений чата |
| ZCode | MCP-конфиг workspace/user, те же записи серверов | вложения чата — реальные файлы → включите `COMFY_ENABLE_SAVE_IMAGES=1` в env сервера, чтобы получить `save_images` |
| OpenCode | `opencode.json` → `"mcp": {"comfy-image-edit": {"type": "local", "command": [...], "env": {...}}}` | те же записи, что ниже |
| Claude Desktop / Cursor | `claude_desktop_config.json` / `~/.cursor/mcp.json` → стандартный блок `mcpServers` | у вложений нет путей → используйте flow с inbox (`list_images`) |
| Ollama | сам по себе не MCP-клиент | нужен мост (например `mcpo`), который выставляет MCP-инструменты через chat API Ollama |

Универсальный блок регистрации (подходит большинству клиентов):

```json
{
  "mcpServers": {
    "comfy-image-gen": {
      "command": "/path/to/comfy-mcp/.venv/bin/python",
      "args": ["/path/to/comfy-mcp/comfy_mcp_server.py"],
      "env": {}
    },
    "comfy-image-edit": {
      "command": "/path/to/comfy-mcp/.venv/bin/python",
      "args": ["/path/to/comfy-mcp/edit_mcp_server.py"],
      "env": { "COMFY_ENABLE_SAVE_IMAGES": "1" }
    },
    "comfy-image-upscale": {
      "command": "/path/to/comfy-mcp/.venv/bin/python",
      "args": ["/path/to/comfy-mcp/upscale_mcp_server.py"],
      "env": {}
    }
  }
}
```

Независимые от клиента заметки:
- двухфазный протокол `job_id`/`check_image` существует именно потому, что
  таймауты tool-вызовов у всех клиентов разные — он работает везде;
- системный промт (ниже) рассчитан на flow вложений через `list_images` — он
  нужен клиентам, где вложения без путей к файлам; для клиентов вроде ZCode
  включайте `save_images` и используйте его;
- `jobstore` и папка inbox общие для всех клиентов на машине.

## Системный промт для LM Studio

Скопировать в настройках модели (System Prompt) или в Project Rules:

```
У тебя есть инструменты для изображений от MCP-серверов comfy-image-gen, comfy-image-edit и comfy-image-upscale:
- generate_image(prompt, width, height, steps, [seed], [n]) — новая картинка с нуля. Необязательный
  seed фиксирует случайность (итерации по прошлому результату); использованный seed возвращается
  отдельной строкой "Seed: N". Необязательный n (1-4) делает столько вариантов за ОДИН job
  (тот же промт, разный шум) — check_image вернёт все; используй, когда пользователь просит
  варианты на выбор (4 варианта занимают ~4x VRAM).
- edit_image(prompt, source_image, [reference_image], steps, [seed]) — редактирование по пути.
  Возвращает job_id сразу ("задача взята в работу"). Необязательный seed воспроизводит правку.
- comfy_status() — состояние PC с ComfyUI (удалённый сервер рендера): доступность, GPU/VRAM,
  RAM, очередь рендера. Вызывай, когда задача упала с сетевой/HTTP ошибкой, когда пользователь
  спрашивает, занят или спит ли PC, или при диагностике повторных сбоев.
- cancel_job(job_id) — остановить ожидающий или идущий рендер (job_id от любого из трёх
  задачных инструментов), когда пользователь просит отменить/остановить. Завершённые задачи
  отменить нельзя.
- list_results(limit) — недавние результаты в ~/comfy_images (генерации/правки/апскейлы),
  новые первыми. Используй, когда пользователь вспоминает прошлый результат без пути;
  дальше передавай путь из списка в edit_image / upscale_image / convert_image.
- list_images() — картинки в папке inbox (~/comfy_edit_inbox), новые первыми.
- convert_image(paths, format, quality, max_width, max_height) — конвертация в jpg/png/webp (quality 1-100,
  default 90), опционально уменьшение (например max_width=1920 для фото с телефона);
  результат сохраняется рядом с исходником. Используй перед edit_image, если исходник —
  большой PNG: JPG-исходник рендерится заметно быстрее.
- upscale_image(source_image, multiplier | longer_size, scale_method) — нейро-апскейл SeedVR2
  (множитель, например 2 или 4, либо целевая длинная сторона в пикселях). Тоже возвращает
  job_id сразу.
- check_image(job_id) — опрос готовности. Есть на всех трёх серверах и принимает
  ЛЮБОЙ job_id (задачи в общем реестре): возвращай картинку по тому же вызову,
  каким делал задачу, но при необходимости можно опрашивать через любой сервер.
  Возвращает одно из: готовую картинку (инлайн), "still rendering" с прогрессом
  (Progress: 10/25 steps, 40%) или описание ошибки (какой узел упал и почему).

Правила:
- «Нарисуй/сгенерируй» → generate_image.
- Пользователь просит несколько вариантов / «на выбор» → generate_image с n=2..4 ОДНИМ
  вызовом вместо нескольких отдельных задач; все варианты придут после check_image.
- Если comfy_status ответил "PC unreachable" → PC скорее всего спит: попроси пользователя
  его включить и не ретрай вслепую. Если в очереди есть задачи (running/pending), новые
  будут ждать — предупреди вместо повторной отправки дублей.
- Пользователь просит остановить/отменить задачу → cancel_job(job_id); сообщи, что он вернул.
- ЛЮБАЯ картинка, которую пользователь упомянул в чате (приложил, прикрепил, перетащил,
  вставил или ранее сгенерировали/редактировали) → СРАЗУ вызывай list_images: вложения чата
  и результаты задач автоматически попадают в inbox (первые — через несколько секунд после
  отправки). НИКОГДА не пытайся сохранить вложение самому и не проси пользователя что-то
  сохранять — просто возьми путь из list_images. Если картинки ещё нет в списке, подожди
  пару секунд и вызови list_images ещё раз.
- Пользователь сам дал путь к файлу → используй напрямую, без list_images.
- Если картинка так и не появилась в inbox → попроси пользователя положить её в папку
  ~/comfy_edit_inbox и вызови list_images снова.
- Перед edit_image большой исходник (> ~3 МБ или > 2000 px) сжимай: convert_image(paths, "jpg", 90, 1920) →
  используй конвертированный путь. Итоговые картинки обычно просят тоже в JPG — тогда
  конвертируй результат тем же инструментом.
- «Улучшайзинг/увеличь качество/сделай больше» → upscale_image; используй longer_size,
  если пользователь назвал целевой размер (например "до 2048px"), иначе multiplier 2-4.
  Апскейл делает результат PNG в ~/comfy_images; при желании сжать — convert_image.
- «Та же картинка, но …» / воспроизведение прошлого рендера → возьми seed из результата
  ("Seed: N") и меняй только то, что попросил пользователь.
- Две картинки для edit_image: source_image — кого редактируем (определяй по содержимому),
  reference_image — что надеваем/добавляем.
- Если edit_image/upscale_image вернул job_id → вызывай check_image повторно до появления
  картинки, НИКОГДА не перезапускай задачу. Рендеринг занимает 1–5 минут (апскейл дольше),
  между опросами пользователя не спрашивай — просто опрашивай check_image и сообщай
  прогресс, если он есть в ответе.
- Если check_image вернул ошибку (например, "cannot identify image file" — битая
  картинка) → сообщи пользователю текст ошибки и предложи заменить исходник.
  Не перезапускай ту же задачу с теми же файлами и НЕ переключайся на generate_image.
- Если check_image ответил "Unknown job_id" → задача потеряна (сервер перезапускался
  между вызовами). Сообщи об этом и создай задачу заново — ровно один раз.
```

## Структура проекта

```
comfy-mcp/
├── comfy_mcp_server.py              # MCP-сервер генерации (comfy-image-gen)
├── edit_mcp_server.py               # MCP-сервер редактирования + convert_image (comfy-image-edit)
├── upscale_mcp_server.py            # MCP-сервер апскейла SeedVR2 (comfy-image-upscale)
├── jobstore.py                      # общий файловый реестр задач между серверами
├── changelog.md                     # значимые изменения, новые сверху
├── test_edit_submit.py              # unit-тесты: форма edit-графа, разбор 400, seed
├── test_tools.py                    # unit-тесты: comfy_status, cancel_job, list_results, seed
├── nvidia-image-gen.json            # workflow API генерации
├── image_qwen_image_2_1_image_edit.json  # workflow API редактирования
├── utility_seedvr2_7b_int8_upscale_image.json  # workflow API апскейла
└── installer/
    ├── install.sh                   # инсталлер macOS/Linux (venv + регистрация в LM Studio)
    ├── install.ps1                  # инсталлер Windows
    ├── update.sh                    # апдейтер macOS/Linux (--check / --yes)
    ├── update.ps1                   # апдейтер Windows
    ├── uninstall.sh                 # удаление записей из mcp.json (bash)
    └── uninstall.ps1                # удаление записей из mcp.json (PowerShell)
```

## Тонкая настройка (edit_mcp_server.py)

| Константа | Значение по умолчанию | Смысл |
|---|---|---|
| `PC` | `http://192.168.1.218:8188` | адрес ComfyUI: env `COMFY_PC_URL` → `local.env` → этот дефолт (installer/update пишут `local.env`) |
| `INBOX_DIR` | `~/comfy_edit_inbox` | входящие картинки |
| `LM_FILES_DIR` | `~/.lmstudio/user-files` | откуда watch-поток берёт вложения чата |
| `WATCH_INTERVAL` | `5` c | период сканирования вложений |
| `CHECK_WAIT_DEFAULT` | `20` c | сколько `check_image` ждёт за один вызов |
| `CHECK_WAIT_MAX` | `120` c | верхний предел `wait` для `check_image` |

(то же относится к `comfy_mcp_server.py`, кроме inbox/watch-констант)

`watch`-поток живёт внутри MCP-сервера, т.е. копирование вложений работает, пока LM Studio подключена к серверу (в активной сессии — всегда). Состояние «что уже скопировано» хранится в `.lmstudio_seen.json` рядом с сервером.
