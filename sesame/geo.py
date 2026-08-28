"""Geodesic helpers for constant-speed path traversal.

The whole point of this module is that movement is parameterised by *arc
length*, not by polyline vertex index.  Vertex spacing therefore has no effect
on the reported speed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

EARTH_RADIUS_M = 6371008.8

LatLon = tuple[float, float]


def haversine(a: LatLon, b: LatLon) -> float:
    """Great-circle distance between two ``(lat, lon)`` points, in meters."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def bearing(a: LatLon, b: LatLon) -> float:
    """Initial bearing from ``a`` to ``b``, in degrees clockwise from north."""
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlon = math.radians(b[1] - a[1])
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def interpolate(a: LatLon, b: LatLon, fraction: float) -> LatLon:
    """Point ``fraction`` of the way along the great circle from ``a`` to ``b``.

    Falls back to a linear blend when the endpoints are nearly coincident,
    where the spherical formula loses precision.
    """
    if fraction <= 0.0:
        return a
    if fraction >= 1.0:
        return b

    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    d = haversine(a, b) / EARTH_RADIUS_M
    if d < 1e-9:
        return (a[0] + (b[0] - a[0]) * fraction, a[1] + (b[1] - a[1]) * fraction)

    sin_d = math.sin(d)
    p = math.sin((1 - fraction) * d) / sin_d
    q = math.sin(fraction * d) / sin_d
    x = p * math.cos(lat1) * math.cos(lon1) + q * math.cos(lat2) * math.cos(lon2)
    y = p * math.cos(lat1) * math.sin(lon1) + q * math.cos(lat2) * math.sin(lon2)
    z = p * math.sin(lat1) + q * math.sin(lat2)
    return (
        math.degrees(math.atan2(z, math.hypot(x, y))),
        math.degrees(math.atan2(y, x)),
    )


def offset(point: LatLon, distance_m: float, bearing_deg: float) -> LatLon:
    """Move ``point`` by ``distance_m`` along ``bearing_deg``."""
    if distance_m == 0.0:
        return point
    lat1, lon1 = math.radians(point[0]), math.radians(point[1])
    theta = math.radians(bearing_deg)
    d = distance_m / EARTH_RADIUS_M
    lat2 = math.asin(math.sin(lat1) * math.cos(d) + math.cos(lat1) * math.sin(d) * math.cos(theta))
    lon2 = lon1 + math.atan2(
        math.sin(theta) * math.sin(d) * math.cos(lat1),
        math.cos(d) - math.sin(lat1) * math.sin(lat2),
    )
    return (math.degrees(lat2), (math.degrees(lon2) + 540.0) % 360.0 - 180.0)


@dataclass(frozen=True)
class Path:
    """A polyline with a precomputed cumulative-length table.

    ``points`` keeps every vertex the user drew; ``cumulative[i]`` is the
    distance in meters from the start of the path to ``points[i]``.
    """

    points: tuple[LatLon, ...]
    cumulative: tuple[float, ...]

    @classmethod
    def from_points(cls, points: list[LatLon]) -> Path:
        cleaned: list[LatLon] = []
        for point in points:
            # Drop consecutive duplicates; they contribute nothing to arc length
            # and would produce zero-length segments in the lookup.
            if not cleaned or haversine(cleaned[-1], point) > 1e-6:
                cleaned.append((float(point[0]), float(point[1])))
        if not cleaned:
            raise ValueError("path needs at least one point")
        if len(cleaned) == 1:
            return cls(points=tuple(cleaned), cumulative=(0.0,))

        cumulative = [0.0]
        for previous, current in zip(cleaned, cleaned[1:], strict=False):
            cumulative.append(cumulative[-1] + haversine(previous, current))
        return cls(points=tuple(cleaned), cumulative=tuple(cumulative))

    @property
    def length(self) -> float:
        return self.cumulative[-1]

    def at(self, distance_m: float) -> tuple[LatLon, float]:
        """Position and heading at ``distance_m`` along the path.

        Distances outside ``[0, length]`` clamp to the endpoints, so callers
        can advance past the end and detect completion themselves.
        """
        if len(self.points) == 1:
            return self.points[0], 0.0
        if distance_m <= 0.0:
            return self.points[0], bearing(self.points[0], self.points[1])
        if distance_m >= self.length:
            return self.points[-1], bearing(self.points[-2], self.points[-1])

        index = _bisect_right(self.cumulative, distance_m) - 1
        index = min(index, len(self.points) - 2)
        start, end = self.points[index], self.points[index + 1]
        segment = self.cumulative[index + 1] - self.cumulative[index]
        fraction = 0.0 if segment <= 0 else (distance_m - self.cumulative[index]) / segment
        return interpolate(start, end, fraction), bearing(start, end)


def _bisect_right(values: tuple[float, ...], target: float) -> int:
    low, high = 0, len(values)
    while low < high:
        mid = (low + high) // 2
        if target < values[mid]:
            high = mid
        else:
            low = mid + 1
    return low
