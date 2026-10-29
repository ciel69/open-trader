"""Записывает золотой файл guard (запускать ТОЛЬКО на guard без уровней риска; файл уже создан до правок WP16 и не перезаписывается тестами)."""
import json
import os
import sys

import _bootstrap  # noqa: F401
import _wp16_scen as S

ROOT = _bootstrap.ROOT
if __name__ == "__main__":
    dst = os.path.join(ROOT, "tests", "fixtures_wp16", "guard_golden.json")
    if os.path.exists(dst) and "--force" not in sys.argv:
        sys.exit("золотой файл уже есть: %s (--force только осознанно)" % dst)
    root = S.make_root(False, ROOT)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(S.run_all(root), f, ensure_ascii=False, sort_keys=True, indent=0)
    print("записано", dst)
