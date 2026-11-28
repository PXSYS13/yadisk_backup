"""Служебные папки бэкапа не попадают ни в сортировку по людям, ни в загрузку в Безлимит.

В «_превью» лежат 1280-пиксельные превью вместо отложенных оригиналов (люди уже принимали их
за испорченные фото), в «_ссылки» — чужие файлы по публичным ссылкам, в «_заменённые» —
повреждённые копии, отложенные проверкой. Списки модулей сверяются с константами utils.
"""

from __future__ import annotations

import pytest

import utils

SERVICE_DIRS = (utils.PREVIEW_DIRNAME, utils.LINKS_DIRNAME, utils.SET_ASIDE_DIRNAME, "_unlim_tmp_zips")


@pytest.mark.parametrize("name", SERVICE_DIRS)
def test_uploader_skips_backup_service_dirs(name):
    from uploader import media

    assert name.casefold() in media.SKIP_DIR_NAMES


@pytest.mark.parametrize("name", SERVICE_DIRS)
def test_people_sorter_skips_backup_service_dirs(name):
    pytest.importorskip("PIL")
    from sorter import media_scan

    assert name.casefold() in media_scan.SKIP_DIR_NAMES
