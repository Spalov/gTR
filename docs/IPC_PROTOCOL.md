# Локальный протокол IPC v1

Одно сообщение занимает одну UTF-8 строку длиной до 1 МиБ. Родитель пишет во входной поток дочернего процесса и читает выходной поток. Диагностика отделена в stderr. Процесс запускает `python -X utf8 -u -m gtr.ipc`. Сервер сокетов отсутствует. Аргументы процесса не содержат секретов.

```json
{"v":1,"id":1,"method":"hello","params":{}}
{"v":1,"id":1,"result":{"protocol":1,"release":"Beta 6"}}
{"v":1,"id":2,"method":"chat.page","params":{"project":"C:/Recovery/Project","chat":1,"limit":80}}
{"v":1,"id":2,"event":"progress","stage":"search","done":0,"total":0}
{"v":1,"id":2,"result":{"records":[],"before":null}}
```

`id` — возрастающее положительное целое до 2⁵³−1. Сначала требуется `hello`. Дубли ключей, неизвестные методы и лишние параметры отклоняются. Сеть не используется для локальных команд. Одновременно работает одна задача. `cancel` принимает ID в поле `target`. Служба отвечает на отмену отдельно. Задача завершится на ближайшей точке отмены. Завершившая публикацию команда может вернуть успешный результат. Прогресс с `total:0` не содержит оценки оставшегося времени.

| Метод | Параметры |
|---|---|
| `project.list`, `operation.list`, `storage.info`, `ui.ready` | Пустой объект |
| `project.open` | `project` |
| `archive.analyze` | `source`, необязательно `timezone` и `format` |
| `chat.page` | `project`, `chat`, необязательно `before`, `query`, `limit` до 80 |
| `media.inspect` | `project`, числовой `asset`. После проверки в ответе локальный путь и факты |
| `recovery.plan` | `project`, `chat`. Необязательные поля `media`, `profile`, `video`, `first`, `last`, `notes`, `language`, `decisions` |
| `recovery.prepare` | Поля плана и явное `confirmed: true` |
| `verify.local` | `bundle`, необязательно `project` и до двух `captures` |
| `cache.clear` | `confirmed: true` |
| `update.check` | Пустой объект. Проверяется только наличие файлов на ПК |
| `update.install` | `path` к ZIP и `confirmed: true`. Сверяется официальный SHA-256 |
| `update.launch` | `path` к установленной версии, `confirmed: true`, необязательно `project` |

В совместимых вариантах проигрывателя путь поступает из SQLite после хеша и проверки локального тома. Вложения целиком и сессии через JSON не передаются. Ошибка имеет форму `"error":{"code":"UNRESOLVED_ATTACHMENT"}`. Другие коды включают `VERSION_MISMATCH`, `CANCELLED`, `DISK_SPACE`, `RECOVERY_ERROR` и `MEDIA_DECISION_REQUIRED`. Пути и исходные тексты из исключений не передаются. При сбое связи повторный запуск службы требует действия в интерфейсе. Серверная операция по IPC не запускается.
