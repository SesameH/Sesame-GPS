"""Edge cases in the geometry: the inputs a drawn route can actually produce."""

import math

import pytest

from sesame.geo import Path, bearing, haversine, interpolate, offset

TAIPEI = (25.0330, 121.5654)


def test_interpolate_clamps_outside_the_segment():
    a, b = TAIPEI, (25.0430, 121.5654)
    assert interpolate(a, b, -1.0) == a
    assert interpolate(a, b, 0.0) == a
    assert interpolate(a, b, 1.0) == b
    assert interpolate(a, b, 2.0) == b


def test_interpolate_between_near_identical_points():
    # Dragging a vertex a hair produces this; the spherical form loses
    # precision here and the linear fallback has to hold.
    a = TAIPEI
    b = (TAIPEI[0] + 1e-12, TAIPEI[1])
    middle = interpolate(a, b, 0.5)
    assert middle[0] == pytest.approx(a[0], abs=1e-11)
    assert not math.isnan(middle[0])
    assert not math.isnan(middle[1])


def test_offset_by_zero_returns_the_same_point():
    assert offset(TAIPEI, 0.0, 45.0) == TAIPEI


def test_offset_normalises_longitude_across_the_antimeridian():
    # Jitter near the date line must not produce a longitude outside ±180.
    near_line = (0.0, 179.999)
    moved = offset(near_line, 500.0, 90.0)
    assert -180.0 <= moved[1] <= 180.0
    assert haversine(near_line, moved) == pytest.approx(500.0, abs=0.5)


def test_path_needs_at_least_one_point():
    with pytest.raises(ValueError, match="at least one point"):
        Path.from_points([])


def test_path_of_identical_points_collapses_to_one():
    # Clicking the same spot repeatedly.
    path = Path.from_points([TAIPEI, TAIPEI, TAIPEI])
    assert len(path.points) == 1
    assert path.length == 0.0
    assert path.at(0.0) == (TAIPEI, 0.0)
    assert path.at(500.0) == (TAIPEI, 0.0)


def test_path_keeps_points_a_metre_apart():
    # Just above the dedupe threshold: a real, if tiny, segment.
    near = (TAIPEI[0] + 0.00001, TAIPEI[1])
    path = Path.from_points([TAIPEI, near])
    assert len(path.points) == 2
    assert path.length > 0


def test_bearing_is_normalised_to_a_full_circle():
    assert bearing(TAIPEI, (26.0, 121.5654)) == pytest.approx(0.0, abs=0.1)
    assert bearing(TAIPEI, (24.0, 121.5654)) == pytest.approx(180.0, abs=0.1)
    # Due west must come back as 270, never as -90.
    west = bearing(TAIPEI, (25.0330, 120.0))
    assert 0.0 <= west < 360.0
    assert west == pytest.approx(270.0, abs=0.5)


def test_haversine_is_symmetric_and_zero_on_itself():
    a, b = TAIPEI, (35.6762, 139.6503)
    assert haversine(a, a) == 0.0
    assert haversine(a, b) == pytest.approx(haversine(b, a), rel=1e-12)


def test_haversine_handles_antipodes_without_domain_error():
    # asin of a value a hair over 1.0 would raise; the clamp has to hold.
    north, south = (90.0, 0.0), (-90.0, 0.0)
    assert haversine(north, south) == pytest.approx(math.pi * 6371008.8, rel=1e-6)


def test_path_crossing_the_antimeridian_measures_the_short_way():
    west, east = (0.0, 179.9), (0.0, -179.9)
    path = Path.from_points([west, east])
    # Roughly 22 km across the line, not most of the way around the planet.
    assert path.length < 30_000
    middle = path.at(path.length / 2)[0]
    assert abs(middle[1]) > 179.0


def test_path_at_is_monotonic_along_its_length():
    path = Path.from_points([TAIPEI, (25.05, 121.58), (25.02, 121.61)])
    travelled = 0.0
    previous = path.at(0.0)[0]
    for step in range(1, 101):
        point = path.at(path.length * step / 100)[0]
        travelled += haversine(previous, point)
        previous = point
    # Sampling the path must not double back or overshoot its own length.
    assert travelled == pytest.approx(path.length, rel=0.01)


def test_path_from_a_single_point_reports_no_heading():
    path = Path.from_points([TAIPEI])
    assert path.length == 0.0
    assert path.at(0.0) == (TAIPEI, 0.0)
