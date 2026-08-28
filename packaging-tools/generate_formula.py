"""Generate a Homebrew formula from uv.lock.

Homebrew installs Python applications into their own virtualenv and needs every
runtime dependency spelled out as a ``resource`` block. ``uv.lock`` already
pins each one with an sdist URL and sha256, so it is a better source than
``brew update-python-resources`` -- which resolves against PyPI and cannot see
a project that is distributed as a GitHub tarball.
"""

from __future__ import annotations

import argparse
import hashlib
import tomllib
import urllib.request
from pathlib import Path

from packaging.markers import Marker

# The environment the formula will be built in.
MACOS_PYTHON_313 = {
    "sys_platform": "darwin",
    "platform_system": "Darwin",
    "os_name": "posix",
    "platform_machine": "arm64",
    "platform_python_implementation": "CPython",
    "python_version": "3.13",
    "python_full_version": "3.13.0",
    "implementation_name": "cpython",
}

FORMULA_TEMPLATE = """class Sesame < Formula
  include Language::Python::Virtualenv

  desc "{desc}"
  homepage "{homepage}"
  url "{url}"
  sha256 "{sha256}"
  license "MIT"

  depends_on :macos
  depends_on "python@3.13"

{resources}
  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "sesame", shell_output("#{{bin}}/sesame --help")
  end
end
"""


def applies_on_macos(marker: str | None) -> bool:
    if not marker:
        return True
    return Marker(marker).evaluate(MACOS_PYTHON_313)


def runtime_closure(packages: dict[str, dict], root: str) -> set[str]:
    """Every package the app needs at runtime on macOS, markers evaluated."""
    needed: set[str] = set()

    def walk(name: str) -> None:
        name = name.lower()
        if name in needed or name not in packages:
            return
        needed.add(name)
        for dependency in packages[name].get("dependencies", []):
            if applies_on_macos(dependency.get("marker")):
                walk(dependency["name"])

    for dependency in packages[root].get("dependencies", []):
        if applies_on_macos(dependency.get("marker")):
            walk(dependency["name"])
    return needed


def resource_blocks(packages: dict[str, dict], names: set[str]) -> tuple[str, list[str]]:
    blocks: list[str] = []
    skipped: list[str] = []
    for name in sorted(names):
        package = packages[name]
        sdist = package.get("sdist")
        if sdist is None or "url" not in sdist:
            skipped.append(name)
            continue
        blocks.append(
            f'  resource "{package["name"]}" do\n'
            f'    url "{sdist["url"]}"\n'
            f'    sha256 "{sdist["hash"].removeprefix("sha256:")}"\n'
            f"  end\n"
        )
    return "\n".join(blocks), skipped


def tarball_sha256(url: str) -> str:
    digest = hashlib.sha256()
    with urllib.request.urlopen(url) as response:
        for chunk in iter(lambda: response.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--tag", required=True, help="release tag, e.g. v0.1.0")
    parser.add_argument("--repo", default="SesameH/Sesame-GPS")
    parser.add_argument("--out", type=Path, default=Path("Formula/sesame.rb"))
    args = parser.parse_args()

    with args.lock.open("rb") as handle:
        lock = tomllib.load(handle)
    with args.pyproject.open("rb") as handle:
        project = tomllib.load(handle)["project"]

    packages = {package["name"].lower(): package for package in lock["package"]}
    names = runtime_closure(packages, project["name"].lower())
    blocks, skipped = resource_blocks(packages, names)

    url = f"https://github.com/{args.repo}/archive/refs/tags/{args.tag}.tar.gz"
    print(f"hashing {url}")
    formula = FORMULA_TEMPLATE.format(
        desc=project["description"],
        homepage=f"https://github.com/{args.repo}",
        url=url,
        sha256=tarball_sha256(url),
        resources=blocks,
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(formula)
    print(f"wrote {args.out}: {len(names) - len(skipped)} resources")
    if skipped:
        print(f"skipped (no sdist): {', '.join(skipped)}")


if __name__ == "__main__":
    main()
