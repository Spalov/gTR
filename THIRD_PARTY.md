# Сторонние компоненты

Комплект gTR для Windows использует Windows x64 CPython 3.14.10 и открытые пакеты указанных в `requirements-win64.txt` версий. Лицензия Apache 2.0 применяется к исходникам gTR. Сторонние компоненты распространяются на своих условиях.

| Компонент | Условия и исходники |
|---|---|
| CPython 3.14.10 | [Лицензия PSF и лицензии компонентов](https://docs.python.org/3/license.html) (`runtime/LICENSE.txt`), [исходники](https://www.python.org/downloads/release/python-31410/) |
| Qt 6.12.0 | LGPLv3 для используемых библиотек Qt, [исходники Qt 6.12.0](https://download.qt.io/official_releases/qt/6.12/6.12.0/single/), [уведомления о сторонних компонентах Qt](https://doc.qt.io/qt-6/licenses-used-in-qt.html) |
| Qt Multimedia 6.12.0 | Динамические Qt Multimedia и Qt Multimedia Widgets с Windows Media Foundation. [Документация и лицензии модуля](https://doc.qt.io/qt-6/qtmultimedia-index.html) · [Исходники](https://download.qt.io/official_releases/qt/6.12/6.12.0/submodules/) |
| PySide6 Essentials и Shiboken6 6.12.0 | LGPLv3 для этой поставки, [исходники Qt for Python 6.12.0](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.12.0-src/), [лицензии Qt for Python](https://doc.qt.io/qtforpython-6/licenses.html) |
| Beautiful Soup, Soup Sieve, typing_extensions, Telethon, pyaes, rsa, pyasn1, tzdata, pywin32 | Уведомления и метаданные исходных пакетов находятся в каталогах `*.dist-info` внутри `runtime/Lib/site-packages`. |

gTR использует немодифицированные пакеты Qt for Python с PyPI и загружает библиотеки Qt динамически из `runtime/Lib/site-packages`. Пользователи могут заменить их совместимыми версиями и отлаживать изменения в этих библиотеках. Тексты [LGPLv3](licenses/LGPL-3.0.txt) и [GPLv3](licenses/GPL-3.0.txt) входят в комплект. По ссылкам в таблице можно бесплатно скачать исходники версий, использованных в комплекте.

Адаптации GGE входят в исходники gTR по разрешению Gruzin. gFL остаётся самостоятельным проприетарным проектом. Полный экспорт GGE, серверные учётные модули и компоненты шифрованного хранилища gFL отсутствуют в ZIP. [Происхождение](docs/GGE_PROVENANCE.md)

Для преобразований gTR вызывает отдельно установленный FFmpeg и FFprobe. Их бинарники, libx264, PyAV, Pillow и cryptography отсутствуют в Windows ZIP. FFmpeg по умолчанию LGPL. Флаг сборки --enable-gpl и поддержка libx264 меняют условия распространения на GPL. [FFmpeg Legal](https://ffmpeg.org/legal.html)

Опциональный профиль исходников media использует PyAV под BSD-3-Clause на уровне исходного адаптера и Pillow по MIT-CMU. DLL из готовых wheel PyAV могут содержать FFmpeg с GPL-компонентами. Распространение таких wheel требует проверки их конфигурации и уведомлений. Архив GGE тестировался с cryptography из отдельной среды. Код cryptography доступен по Apache-2.0 или BSD-3-Clause. [Сборки FFmpeg для PyAV](https://github.com/PyAV-Org/pyav-ffmpeg) · [Pillow](https://github.com/python-pillow/Pillow/blob/main/LICENSE) · [cryptography](https://github.com/pyca/cryptography/blob/main/LICENSE)
## gAssets Beta 7

Установщик компилируется Inno Setup. GAU использует .NET Framework 4.8, Windows CNG и системные механизмы Windows. Генератор релизов вызывает установленный OpenSSL для подписи на стороне разработчика. Исполняемые файлы OpenSSL в комплект gAssets не входят. Релиз gTR сохраняет уведомления Python, Qt, Telethon и GGE, описанные ниже.

