# Сторонние компоненты

Комплект gTR для Windows использует Windows x64 CPython 3.14.10 и открытые пакеты указанных в `requirements-win64.txt` версий. Лицензия Apache 2.0 применяется к исходникам gTR. Сторонние компоненты распространяются на своих условиях.

| Компонент | Условия и исходники |
|---|---|
| CPython 3.14.10 | [Лицензия PSF и лицензии компонентов](https://docs.python.org/3/license.html) (`runtime/LICENSE.txt`), [исходники](https://www.python.org/downloads/release/python-31410/) |
| Qt 6.12.0 | LGPLv3 для используемых библиотек Qt, [исходники Qt 6.12.0](https://download.qt.io/official_releases/qt/6.12/6.12.0/single/), [уведомления о сторонних компонентах Qt](https://doc.qt.io/qt-6/licenses-used-in-qt.html) |
| PySide6 Essentials и Shiboken6 6.12.0 | LGPLv3 для этой поставки, [исходники Qt for Python 6.12.0](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.12.0-src/), [лицензии Qt for Python](https://doc.qt.io/qtforpython-6/licenses.html) |
| Beautiful Soup, Soup Sieve, typing_extensions, Telethon, pyaes, rsa, pyasn1, tzdata, pywin32 | Уведомления и метаданные исходных пакетов находятся в каталогах `*.dist-info` внутри `runtime/Lib/site-packages`. |

gTR использует немодифицированные пакеты Qt for Python с PyPI и загружает библиотеки Qt динамически из `runtime/Lib/site-packages`. Пользователи могут заменить их совместимыми версиями и отлаживать изменения в этих библиотеках. Тексты [LGPLv3](licenses/LGPL-3.0.txt) и [GPLv3](licenses/GPL-3.0.txt) входят в комплект. По ссылкам в таблице можно бесплатно скачать исходники версий, использованных в комплекте.

Для кадров из видео можно указать установленный FFmpeg. В исходниках gTR доступен интерфейс для стороннего адаптера предпросмотра, в том числе на базе gFL.
