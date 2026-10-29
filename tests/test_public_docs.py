"""Публичные доки для посторонних: ссылки на картинки и соседние документы не битые, личного пути нет, PROVIDERS есть в обоих языках."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = ("README.md", "README.ru.md", "docs/PROVIDERS.md", "docs/PROVIDERS.ru.md", "docs/QUICKSTART.md", "docs/INDEX.md", "docs/images/README.md")
RE_LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class PublicDocs(unittest.TestCase):
    def test_links_resolve(self):
        for rel in DOCS:
            base = os.path.dirname(os.path.join(ROOT, rel))
            for target in RE_LINK.findall(read(rel)):
                if target.startswith(("http://", "https://", "#", "mailto:")):
                    continue
                path = os.path.normpath(os.path.join(base, target.split("#")[0]))
                self.assertTrue(os.path.exists(path), "%s: битая ссылка %s" % (rel, target))

    def test_no_personal_path(self):
        for rel in DOCS:
            txt = read(rel)
            self.assertNotIn("/Users/", txt, rel)
            self.assertNotIn("aleksej", txt.lower(), rel)

    def test_readme_points_to_providers(self):
        self.assertIn("docs/PROVIDERS.md", read("README.md"))
        self.assertIn("docs/PROVIDERS.ru.md", read("README.ru.md"))

    def test_presets_in_providers_doc(self):
        import json
        with open(os.path.join(ROOT, "config", "provider_presets.json"), encoding="utf-8") as f:
            d = json.load(f)
        presets = d.get("presets", d)
        ids = list(presets) if isinstance(presets, dict) else [p.get("id") for p in presets]
        for lang in ("docs/PROVIDERS.md", "docs/PROVIDERS.ru.md"):
            txt = read(lang)
            for pid in ids:
                self.assertIn("`%s`" % pid, txt, "%s: нет шаблона %s" % (lang, pid))


if __name__ == "__main__":
    unittest.main()
