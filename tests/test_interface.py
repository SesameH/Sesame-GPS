"""The interface's string table, checked from outside the browser."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX = Path(__file__).parent.parent / "sesame" / "static" / "index.html"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="needs node to evaluate the string table"
)


def strings():
    """The STRINGS table, evaluated rather than parsed, so it stays honest."""
    source = INDEX.read_text()
    script = re.findall(r"<script>(.*?)</script>", source, re.S)[-1]
    start = script.index("const STRINGS")
    end = script.index("let lang =")
    program = (
        script[start:end]
        + "\nconst flat = (o, p='') => Object.entries(o).flatMap(([k, v]) =>"
        "  (v && typeof v === 'object' && !Array.isArray(v)) ? flat(v, p + k + '.') : [p + k]);"
        "\nconsole.log(JSON.stringify({en: flat(STRINGS.en), zh: flat(STRINGS.zh)}));"
    )
    result = subprocess.run(
        ["node", "-e", program], capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


def test_both_languages_carry_the_same_keys():
    table = strings()
    # A key present in one language only shows up as `undefined` on screen.
    assert sorted(table["en"]) == sorted(table["zh"])
    assert len(table["en"]) > 50


def test_every_key_the_markup_asks_for_exists():
    table = set(strings()["en"])
    used = set(re.findall(r'data-i18n(?:-ph)?="([a-zA-Z]+)"', INDEX.read_text()))
    assert used
    assert used <= table


def test_every_diagnosis_code_can_be_phrased():
    from sesame import engine

    table = strings()
    # Whatever diagnose_wifi can return, the interface has to be able to say.
    codes = set(re.findall(r'"code": "([a-z-]+)"', Path(engine.__file__).read_text()))
    codes.discard("ok")
    for language in ("en", "zh"):
        keyed = {key.split(".", 1)[1] for key in table[language] if key.startswith("diag.")}
        assert codes <= keyed, f"{language} cannot phrase {codes - keyed}"


def test_every_pairing_code_can_be_phrased():
    from sesame import engine

    table = strings()
    codes = set(re.findall(r'PairingError\(\s*"([a-z-]+)"', Path(engine.__file__).read_text()))
    for language in ("en", "zh"):
        keyed = {key.split(".", 1)[1] for key in table[language] if key.startswith("pairErr.")}
        assert codes <= keyed, f"{language} cannot phrase {codes - keyed}"


def test_the_map_opens_over_san_jose():
    centre = re.search(r"setView\(\[([-\d.]+), ([-\d.]+)\]", INDEX.read_text())
    latitude, longitude = float(centre.group(1)), float(centre.group(2))
    assert latitude == pytest.approx(37.34, abs=0.2)
    assert longitude == pytest.approx(-121.89, abs=0.2)


def test_english_is_the_default():
    source = INDEX.read_text()
    assert "localStorage.getItem('sesame.lang') || 'en'" in source
    assert "if (!STRINGS[lang]) lang = 'en';" in source
