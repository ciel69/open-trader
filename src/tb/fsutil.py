"""Мелкие кроссплатформенные помощники для файлов.

replace(): os.replace с повторами. На Windows замена файла падает с PermissionError, пока другой процесс (например,
дашборд) держит его открытым на чтение; на macOS/Linux повторы не нужны и не срабатывают.
"""
import os
import time


def replace(src, dst, attempts=20, delay=0.05):
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)
