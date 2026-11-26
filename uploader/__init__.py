"""Загрузчик фото и видео с компьютера в безлимитное хранилище Яндекс.Диска (/photounlim).

Карта модулей:
  paths           единые пути файлов программы (перенаправляются в тестах)
  utils           хеширование за один проход (MD5+SHA256), длинные пути, SQLite-транзакции, коды выхода
  cookies         парсер cookies.json (6 форматов), сессия disk.yandex.*, получение sk/uid, вызовы models-v2
  media           фильтр фото/видео (без RAW/DNG), проверка сигнатур (magic bytes), чтение EXIF и MP4 mvhd
  state           SQLite WAL база upload_state.db: очередь, локальная и облачная дедупликация, статусы
  remote_index    индексация текущего содержимого /photounlim через mpfs/list
  quota_guard     проверка квоты (mpfs/space, mpfs/get-user-info), лимит размера файла, защита used_space
  transport_base  единый контракт транспорта загрузки
  transport_web   прямой транспорт через models-v2 (mpfs/store -> hardlink / resumable PUT -> operation-status)
  engine          сканирование папки, очередь воркеров, обработка 429/капчи/сбоев, watch-режим
  webapp          FastAPI-сервер и управление фоновой задачей для локального веб-интерфейса
"""

from __future__ import annotations

APP_VERSION = "1.0.0"
APP_ID = "yadisk-unlim-uploader"
