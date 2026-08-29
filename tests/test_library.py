"""The saved-route library, over a scratch file."""

import json

import pytest

from sesame.library import (
    LibraryError,
    delete_route,
    load_routes,
    rename_route,
    save_route,
)

POINTS = [[25.0330, 121.5654], [25.0430, 121.5654]]
OTHER = [[37.3382, -121.8863], [37.3482, -121.8863], [37.3482, -121.8763]]


def test_a_saved_route_comes_back_whole():
    saved = save_route("Morning loop", POINTS)
    assert saved["name"] == "Morning loop"
    assert saved["points"] == POINTS
    assert load_routes() == [saved]


def test_saving_over_a_name_replaces_that_save_and_keeps_its_id():
    first = save_route("Morning loop", POINTS)
    second = save_route("Morning loop", OTHER)
    assert second["id"] == first["id"]
    assert load_routes() == [second]


def test_saves_are_listed_in_the_order_they_were_made():
    save_route("first", POINTS)
    save_route("second", OTHER)
    assert [route["name"] for route in load_routes()] == ["first", "second"]


def test_renaming_leaves_the_points_alone():
    saved = save_route("typo", POINTS)
    renamed = rename_route(saved["id"], "Morning loop")
    assert renamed["name"] == "Morning loop"
    assert renamed["points"] == POINTS
    assert [route["name"] for route in load_routes()] == ["Morning loop"]


def test_renaming_onto_another_saves_name_is_refused():
    save_route("Morning loop", POINTS)
    other = save_route("Evening loop", OTHER)
    with pytest.raises(LibraryError) as raised:
        rename_route(other["id"], "Morning loop")
    assert raised.value.code == "name-taken"
    assert [route["name"] for route in load_routes()] == ["Morning loop", "Evening loop"]


def test_renaming_a_save_to_what_it_already_is_works():
    saved = save_route("Morning loop", POINTS)
    assert rename_route(saved["id"], "Morning loop")["name"] == "Morning loop"


def test_a_name_of_only_spaces_is_refused():
    with pytest.raises(LibraryError) as raised:
        save_route("   ", POINTS)
    assert raised.value.code == "empty-name"


def test_runs_of_whitespace_in_a_name_collapse():
    # Two saves that look identical in the picker would be unusable.
    assert save_route("Morning\n  loop ", POINTS)["name"] == "Morning loop"


def test_deleting_removes_only_that_save():
    save_route("Morning loop", POINTS)
    doomed = save_route("Evening loop", OTHER)
    delete_route(doomed["id"])
    assert [route["name"] for route in load_routes()] == ["Morning loop"]


def test_acting_on_a_save_that_is_gone_says_so():
    for act in (lambda: delete_route("nope"), lambda: rename_route("nope", "new")):
        with pytest.raises(LibraryError) as raised:
            act()
        assert raised.value.code == "not-found"


def test_a_missing_library_reads_as_empty():
    assert load_routes() == []


def test_a_damaged_library_reads_as_empty_rather_than_raising(route_library):
    route_library.write_text("{ this is not json")
    assert load_routes() == []


def test_entries_that_could_not_be_drawn_are_skipped(route_library):
    save_route("Morning loop", POINTS)
    kept = json.loads(route_library.read_text())
    route_library.write_text(
        json.dumps(
            [
                {"id": "a", "name": "one point", "points": [[1.0, 2.0]]},
                {"id": "b", "name": "no points", "points": []},
                {"id": "c", "points": POINTS},
                "not even a route",
                *kept,
            ]
        )
    )
    assert [route["name"] for route in load_routes()] == ["Morning loop"]


def test_a_half_written_library_cannot_replace_a_good_one(route_library, monkeypatch):
    save_route("Morning loop", POINTS)

    def die(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(json, "dump", die)
    with pytest.raises(OSError):
        save_route("Evening loop", OTHER)
    assert [route["name"] for route in load_routes()] == ["Morning loop"]
