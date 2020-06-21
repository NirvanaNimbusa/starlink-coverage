#!/usr/bin/env python3

from types import SimpleNamespace
from skyfield import api
from skyfield.api import Loader

import sys
import math
import json
import itertools
from collections import defaultdict

from typing import List, Dict, DefaultDict, Set, Tuple

import s2sphere

import numpy as np
import matplotlib.pyplot as plt
import cartopy.crs as ccrs
import cartopy.io.img_tiles as cimgt
import shapely.geometry
from shapely.geometry import Polygon
import geog
import h3

TLE_URL = 'https://celestrak.com/NORAD/elements/starlink.txt'
R_MEAN = 6378.1  # km
H3_RESOLUTION_LEVEL = 4
process = int(sys.argv[1])


def to_deg(radians: float) -> float:
    return radians * (180 / math.pi)


def to_rads(degrees: float) -> float:
    return degrees * (math.pi / 180)


RIGHT_ANGLE = to_rads(90)


def load_sats() -> List:
    load = Loader('./tle_cache')
    sats = load.tle_file(url=TLE_URL)
    return sats


def filter_sats(sats: List) -> List:
    """From https://www.space-track.org/documentation#faq filter to our best
    understanding of what the operational satellites are"""
    op_sats: List = []
    for sat in sats:
        n = (sat.model.no_kozai / (2 * math.pi)) * 1440
        e = sat.model.ecco
        period = 1440/n
        mu = 398600.4418  # earth grav constant
        a = (mu/(n*2*math.pi/(24*3600)) ** 2) ** (1./3.)  # semi-major axis
        # Using semi-major axis "a", eccentricity "e", and the Earth's radius in km,
        perigee = (a * (1 - e)) - 6378.135
        if perigee > 540:
            op_sats.append(sat)
    return op_sats


def calcAreaSpherical(altitude: float, term_angle: float) -> float:
    """Calculates the area of a Starlink satellite using the
    spherical earth model, a satellite (for altitude), and
    minimum terminal angle (elevation angle)
    """
    epsilon = to_rads(term_angle)

    eta_FOV = math.asin((math.sin(epsilon + RIGHT_ANGLE)
                         * R_MEAN) / (R_MEAN + altitude))

    lambda_FOV = 2 * (math.pi - (epsilon + RIGHT_ANGLE + eta_FOV))

    area = 2 * math.pi * (R_MEAN ** 2) * (1 - math.cos(lambda_FOV / 2))

    return area


def calcCapAngle(altitude: float, term_angle: float) -> float:
    """Returns the cap angle (lambda_FOV/2) in radians"""
    epsilon = to_rads(term_angle)

    eta_FOV = math.asin((math.sin(epsilon + RIGHT_ANGLE)
                         * R_MEAN) / (R_MEAN + altitude))

    lambda_FOV = 2 * (math.pi - (epsilon + RIGHT_ANGLE + eta_FOV))

    return (lambda_FOV / 2)


def get_cell_ids(lat, lng, angle):
    """angle in degrees, is theta (the cap opening angle)"""
    region = s2sphere.Cap.from_axis_angle(s2sphere.LatLng.from_degrees(
        lat, lng).to_point(), s2sphere.Angle.from_radians(angle))
    coverer = s2sphere.RegionCoverer()
    coverer.min_level = 9
    coverer.max_level = 9
    cells = coverer.get_covering(region)
    return cells


def plotFootprint(sat):
    """Uses cartopy to replot the footprint, mostly used for debugging and validating
    math and library usage"""
    angle = calcCapAngle(sat.elevation.km, 35)
    cells = get_cell_ids(sat.latitude.degrees, sat.longitude.degrees, angle)
    print(len(cells))

    proj = cimgt.Stamen('terrain-background')
    plt.figure(figsize=(6, 6), dpi=400)
    ax = plt.axes(projection=proj.crs)
    ax.add_image(proj, 6)
    ax.set_extent([sat.longitude.degrees-10., sat.longitude.degrees+10.,
                   sat.latitude.degrees-10,  sat.latitude.degrees+10.], crs=ccrs.Geodetic())
    ax.background_patch.set_visible(False)

    geoms = []
    for cellid in cells:
        new_cell = s2sphere.Cell(cellid)
        vertices = []
        for i in range(0, 4):
            vertex = new_cell.get_vertex(i)
            latlng = s2sphere.LatLng.from_point(vertex)
            vertices.append((latlng.lng().degrees,
                             latlng.lat().degrees))
        geo = Polygon(vertices)
        geoms.append(geo)

    ax.add_geometries(geoms, crs=ccrs.Geodetic(), facecolor='red',
                      edgecolor='black', alpha=0.4)
    ax.plot(sat.longitude.degrees, sat.latitude.degrees, marker='o', color='red', markersize=4,
            alpha=0.7, transform=ccrs.Geodetic())
    plt.savefig('test.png')


def split_antimeridian_polygon(polygon: List[List[float]]) -> Tuple[List, List]:
    """Takes a GeoJSON formatted list of vertex coordinates List[[lon,lat]]
    and checks if the longitude crosses over the antimeridian. It splits the polygon
    in 2 at the antimeridian.
    """

    # We split the polygon into 2. lon < 180 goes into poly1
    poly1, poly2 = [], []
    has_split = False
    for idx in range(len(polygon)-1):
        first = polygon[idx]
        second = polygon[idx+1]
        if (first[0] < 180 and second[0] > 180) or (first[0] > 180 and second[0] < 180):
            # split
            new_lat = np.interp([180.0], np.array([first[0], second[0]]),
                                np.array([first[1], second[1]]))[0]
            new_point = [180.0, float(new_lat)]
            poly1.append([180.0, float(new_lat)])
            poly2.append([180.0, float(new_lat)])
        elif first[0] < 180:
            poly1.append(first)
        else:
            poly2.append(first)
    # GeoJSON polygons must have the same point for the first and last vertex
    poly1.append(poly1[0])
    poly2.append(poly2[0])
    # h3 doesn't like longitudes > 180 so make them the negative equivalent
    poly2_wrapped: List[List[float]] = []
    for point in poly2:
        poly2_wrapped.append([-180 + (point[0]-180), point[1]])
    mapping1 = shapely.geometry.mapping(shapely.geometry.Polygon(poly1))
    mapping2 = shapely.geometry.mapping(
        shapely.geometry.polygon.orient(shapely.geometry.Polygon(poly2_wrapped), 1.0))

    return (mapping1, mapping2)


def get_cell_ids_h3(lat: float, lng: float, angle: float) -> Set:
    p = shapely.geometry.Point([lng, lat])
    # so to more accurately match projections maybe arc length of a sphere would be best?
    arc_length = R_MEAN * angle  # in km

    n_points = 20
    # arc_length should be in kilometers so convert to meters
    d = arc_length * 1000  # meters
    angles = np.linspace(0, 360, n_points)
    polygon = geog.propagate(p, angles, d)
    try:
        mapping = shapely.geometry.mapping(shapely.geometry.Polygon(polygon))
    except ValueError as e:
        print(f"lat:{lat}, lng:{lng}")
        print(polygon)

    cells = set()
    needs_split = False

    for point in polygon:
        if point[0] > 180:
            needs_split = True
            break

    if needs_split:
        try:
            (first, second) = split_antimeridian_polygon(polygon)
            cells.update(h3.polyfill(first, H3_RESOLUTION_LEVEL, True))
            cells.update(h3.polyfill(second, H3_RESOLUTION_LEVEL, True))
        except:
            print(f"lat:{lat}, lng:{lng}")
    else:
        cells = h3.polyfill(mapping, H3_RESOLUTION_LEVEL, True)

    return cells


def plotFootprintH3(sat, h3_cells):
    """Uses cartopy to replot the footprint, mostly used for debugging and validating
    math and library usage
    """
    angle = calcCapAngle(sat.elevation.km, 35)

    proj = cimgt.Stamen('terrain-background')
    plt.figure(figsize=(6, 6), dpi=400)
    ax = plt.axes(projection=ccrs.PlateCarree(central_longitude=180))
    ax.add_image(proj, 6)
    ax.set_extent([sat.longitude.degrees-10., sat.longitude.degrees+10.,
                   sat.latitude.degrees-10,  sat.latitude.degrees+10.], crs=ccrs.Geodetic())
    ax.background_patch.set_visible(False)

    geoms = []
    for cellid in h3_cells:
        vertices = []
        bounds = h3.h3_to_geo_boundary(cellid)  # arrays of [lat, lng]
        coords = [[lng, lat] for [lat, lng] in bounds]
        geo = Polygon(coords)
        geoms.append(geo)

    ax.add_geometries(geoms, crs=ccrs.Geodetic(), facecolor='red',
                      edgecolor='black', alpha=0.4)
    ax.plot(sat.longitude.degrees, sat.latitude.degrees, marker='o', color='red', markersize=4,
            alpha=0.7, transform=ccrs.Geodetic())
    plt.savefig('test_h3.png')


sats = load_sats()
print(f"Loaded {len(sats)} satellites")

op_sats = filter_sats(sats)
print(f"Filtered to {len(op_sats)} operational satellites")
ts = api.load.timescale()
now = ts.now()

subpoints = {sat.name: sat.at(now).subpoint() for sat in op_sats}

sat1 = subpoints['STARLINK-1284']
angle = calcCapAngle(sat1.elevation.km, 35)

coverage: DefaultDict[str, int] = defaultdict(int)


def readTokens():
    with open('cell_ids.txt', 'r') as fd:
        lines = fd.readlines()
        for line in lines:
            tok = line.strip()
            coverage[tok] = 0


def readH3Indices() -> List[str]:
    with open('h3_5_index.txt', 'r') as fd:
        lines = [line.strip() for line in fd.readlines()]
    return lines


TIME_PER_PROCESS = 1440 // 4  # 360 minutes, a quarter of a day
START_TIME = process * TIME_PER_PROCESS

for i in range(TIME_PER_PROCESS):
    time = ts.utc(2020, 6, 21, 0, START_TIME+i, 0)
    if i % 30 == 0:
        print(time.utc_iso())
    subpoints = {sat.name: sat.at(time).subpoint() for sat in op_sats}
    coverage_set: Set[str] = set()
    for sat_name, sat in subpoints.items():
        angle = calcCapAngle(sat.elevation.km, 35)
        cells = get_cell_ids_h3(sat.latitude.degrees,
                                sat.longitude.degrees, angle)
        if len(cells) == 0:
            Exception("empty region returned")
        for cell in cells:
            coverage_set.add(cell)
    for cell in coverage_set:
        coverage[cell] += 1

with open(f"h3_{H3_RESOLUTION_LEVEL}_cov_{process}.txt", "w") as fd:
    for cell, cov in coverage.items():
        fd.write(f"{cell},{cov}\n")
