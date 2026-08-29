"""Saved routes, kept as one JSON file next to the remembered devices."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path as FilePath

LIBRARY_PATH = FilePath.home() / ".sesame" / "routes.json"

MAX_NAME_LENGTH = 60


class LibraryError(RuntimeError):
    """A saved-route operation the interface is expected to explain.

    ``code`` is ``empty-name`` when a name is nothing but whitespace,
    ``name-taken`` when a rename would leave two saves indistinguishable in the
    picker, and ``not-found`` when the save is already gone -- another window,
    or a hand-edited library file.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def load_routes() -> list[dict]:
    """Every saved route, oldest first. A damaged library reads as empty."""
    try:
        with LIBRARY_PATH.open() as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [route for route in data if _usable(route)]


def save_route(name: str, points: Sequence[Sequence[float]]) -> dict:
    """Store ``points`` under ``name``, replacing a save that already has it.

    Saving over a name keeps its id, so a window holding the old entry selected
    still points at the same save.
    """
    name = _clean(name)
    routes = load_routes()
    at = next((index for index, route in enumerate(routes) if route["name"] == name), None)
    entry = {
        "id": routes[at]["id"] if at is not None else uuid.uuid4().hex,
        "name": name,
        "points": [[float(latitude), float(longitude)] for latitude, longitude in points],
        "savedAt": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    if at is None:
        routes.append(entry)
    else:
        routes[at] = entry
    _write(routes)
    return entry


def rename_route(route_id: str, name: str) -> dict:
    """Give one save a different name."""
    name = _clean(name)
    routes = load_routes()
    at = _locate(routes, route_id)
    if any(route["name"] == name and route["id"] != route_id for route in routes):
        raise LibraryError("name-taken", f"another save is already called {name!r}")
    routes[at] = {**routes[at], "name": name}
    _write(routes)
    return routes[at]


def delete_route(route_id: str) -> None:
    """Forget one save."""
    routes = load_routes()
    del routes[_locate(routes, route_id)]
    _write(routes)


def _locate(routes: list[dict], route_id: str) -> int:
    for index, route in enumerate(routes):
        if route["id"] == route_id:
            return index
    raise LibraryError("not-found", f"no saved route with id {route_id!r}")


def _clean(name: str) -> str:
    # Runs of whitespace collapse so two saves cannot look identical in the
    # picker while counting as different names.
    name = " ".join(name.split())[:MAX_NAME_LENGTH]
    if not name:
        raise LibraryError("empty-name", "a save needs a name")
    return name


def _usable(route: object) -> bool:
    """Whether an entry can be listed and played: named, identified, drawable."""
    if not isinstance(route, dict):
        return False
    if not isinstance(route.get("id"), str) or not isinstance(route.get("name"), str):
        return False
    points = route.get("points")
    if not isinstance(points, list) or len(points) < 2:
        return False
    return all(
        isinstance(point, list)
        and len(point) == 2
        and all(isinstance(value, int | float) and not isinstance(value, bool) for value in point)
        for point in points
    )


def _write(routes: list[dict]) -> None:
    LIBRARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Written alongside and moved into place: dying mid-write leaves the
    # previous library whole rather than truncated.
    temporary = LIBRARY_PATH.with_suffix(".json.tmp")
    with temporary.open("w") as handle:
        json.dump(routes, handle, indent=2)
    os.replace(temporary, LIBRARY_PATH)
