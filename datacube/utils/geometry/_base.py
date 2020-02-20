import functools
import itertools
import math
from collections import namedtuple, OrderedDict
from typing import Tuple, Callable, Iterable, List

import cachetools
import numpy
from affine import Affine
from osgeo import ogr
from pyproj import CRS as _CRS
from pyproj.transformer import Transformer
from pyproj.exceptions import CRSError

from .tools import roi_normalise, roi_shape, is_affine_st
from ..math import is_almost_int

Coordinate = namedtuple('Coordinate', ('values', 'units', 'resolution'))
_BoundingBox = namedtuple('BoundingBox', ('left', 'bottom', 'right', 'top'))

# pylint: disable=too-many-lines


class BoundingBox(_BoundingBox):
    """Bounding box, defining extent in cartesian coordinates.
    """

    def buffered(self, ybuff, xbuff):
        """
        Return a new BoundingBox, buffered in the x and y dimensions.

        :param ybuff: Y dimension buffering amount
        :param xbuff: X dimension buffering amount
        :return: new BoundingBox
        """
        return BoundingBox(left=self.left - xbuff, right=self.right + xbuff,
                           top=self.top + ybuff, bottom=self.bottom - ybuff)

    @property
    def width(self):
        return self.right - self.left

    @property
    def height(self):
        return self.top - self.bottom

    @property
    def points(self) -> List[Tuple[float, float]]:
        """Extract four corners of the bounding box
        """
        x0, y0, x1, y1 = self
        return list(itertools.product((x0, x1), (y0, y1)))

    def transform(self, transform: Affine) -> 'BoundingBox':
        """Transform bounding box through a linear transform

           Apply linear transform on 4 points of the bounding box and compute
           bounding box of these four points.
        """
        pts = [transform*pt for pt in self.points]
        xx = [x for x, _ in pts]
        yy = [y for _, y in pts]
        return BoundingBox(min(xx), min(yy), max(xx), max(yy))


@cachetools.cached({})
def _make_crs(crs_str):
    return _CRS.from_user_input(crs_str)


def _guess_crs_str(crs_spec):
    """
    Returns a string representation of the crs spec.
    Returns `None` if it does not understand the spec.
    """
    if isinstance(crs_spec, str):
        return crs_spec
    if hasattr(crs_spec, 'to_epsg'):
        return 'EPSG:{}'.format(crs_spec.to_epsg())
    if hasattr(crs_spec, 'to_wkt'):
        return crs_spec.to_wkt()
    return None


class CRS(object):
    """
    Wrapper around `pyproj.CRS` for backwards compatibility.
    """

    def __init__(self, crs_str):
        """
        :param crs_str: string representation of a CRS, often an EPSG code like 'EPSG:4326'
        :raises: `pyproj.exceptions.CRSError`
        """
        self.crs_str = _guess_crs_str(crs_str)
        if self.crs_str is None:
            raise CRSError("Expect string or any object with `.to_epsg()` or `.to_wkt()` method")

        self._crs = _make_crs(self.crs_str)

    def __getstate__(self):
        return {'crs_str': self.crs_str}

    def __setstate__(self, state):
        self.__init__(state['crs_str'])

    def to_wkt(self):
        """
        WKT representation of the CRS

        :type: str
        """
        return self._crs.to_wkt()

    @property
    def wkt(self):
        return self.to_wkt()

    def to_epsg(self):
        """
        EPSG Code of the CRS or None

        :type: int | None
        """
        return self._crs.to_epsg()

    @property
    def epsg(self):
        return self.to_epsg()

    @property
    def semi_major_axis(self):
        return self._crs.ellipsoid.semi_major_metre

    @property
    def semi_minor_axis(self):
        return self._crs.ellipsoid.semi_minor_metre

    @property
    def inverse_flattening(self):
        return self._crs.ellipsoid.inverse_flattening

    @property
    def geographic(self):
        """
        :type: bool
        """
        return self._crs.is_geographic

    @property
    def projected(self):
        """
        :type: bool
        """
        return self._crs.is_projected

    @property
    def dimensions(self):
        """
        List of dimension names of the CRS.
        The ordering of the names is intended to reflect the `numpy` array axis order of the loaded raster.

        :type: (str, str)
        """
        if self.geographic:
            return 'latitude', 'longitude'

        if self.projected:
            return 'y', 'x'

        raise ValueError('Neither projected nor geographic')

    @property
    def units(self):
        """
        List of dimension units of the CRS.
        The ordering of the units is intended to reflect the `numpy` array axis order of the loaded raster.

        :type: (str,str)
        """
        if self.geographic:
            return 'degrees_north', 'degrees_east'

        if self.projected:
            x, y = self._crs.axis_info
            return x.unit_name, y.unit_name

        raise ValueError('Neither projected nor geographic')

    def __str__(self):
        return self.crs_str

    def __hash__(self):
        return hash(self.to_wkt())

    def __repr__(self):
        return "CRS('%s')" % self.crs_str

    def __eq__(self, other):
        if other is self:
            return True
        if isinstance(other, CRS):
            return self._crs == other._crs

        crs_str = _guess_crs_str(other)
        if crs_str is None:
            return False
        return self._crs == CRS(crs_str)._crs

    def __ne__(self, other):
        return not (self == other)


def mk_osr_point_transform(src_crs, dst_crs):
    return Transformer.from_crs(src_crs._crs, dst_crs._crs)


def mk_point_transformer(src_crs: CRS, dst_crs: CRS) -> Callable[
        [numpy.ndarray, numpy.ndarray],
        Tuple[numpy.ndarray, numpy.ndarray]]:
    """

    :returns: Function that maps X,Y -> X',Y' where X,Y are coordinates in
              src_crs stored in ndarray of any shape and X',Y' are same shape
              but in dst CRS.
    """
    return Transformer.from_crs(src_crs._crs, dst_crs._crs)


###################################################
# Helper methods to build ogr.Geometry from geojson
###################################################


def _make_point(pt):
    geom = ogr.Geometry(ogr.wkbPoint)
    # Ignore the third dimension
    geom.AddPoint_2D(*pt[0:2])
    return geom


def _make_multi(type_, maker, coords):
    geom = ogr.Geometry(type_)
    for coord in coords:
        geom.AddGeometryDirectly(maker(coord))
    return geom


def _make_linear(type_, coordinates):
    geom = ogr.Geometry(type_)
    for pt in coordinates:
        # Ignore the third dimension
        geom.AddPoint_2D(*pt[0:2])
    return geom


def _make_multipoint(coordinates):
    return _make_multi(ogr.wkbMultiPoint, _make_point, coordinates)


def _make_line(coordinates):
    return _make_linear(ogr.wkbLineString, coordinates)


def _make_multiline(coordinates):
    return _make_multi(ogr.wkbMultiLineString, _make_line, coordinates)


def _make_polygon(coordinates):
    return _make_multi(ogr.wkbPolygon, functools.partial(_make_linear, ogr.wkbLinearRing), coordinates)


def _make_multipolygon(coordinates):
    return _make_multi(ogr.wkbMultiPolygon, _make_polygon, coordinates)


###################################################
# Helper methods to build ogr.Geometry from geojson
###################################################


def _get_coordinates(geom):
    """
    recursively extract coordinates from geometry
    """
    if geom.GetGeometryType() == ogr.wkbPoint:
        return geom.GetPoint_2D(0)
    if geom.GetGeometryType() in [ogr.wkbMultiPoint, ogr.wkbLineString, ogr.wkbLinearRing]:
        return geom.GetPoints()
    else:
        return [_get_coordinates(geom.GetGeometryRef(i)) for i in range(geom.GetGeometryCount())]


def _make_geom_from_ogr(geom, crs):
    if geom is None:
        return None
    result = Geometry.__new__(Geometry)
    result._geom = geom  # pylint: disable=protected-access
    result.crs = crs
    return result


#############################################
# Helper methods to wrap ogr.Geometry methods
#############################################


def _wrap_binary_bool(method):
    @functools.wraps(method, assigned=('__doc__', ))
    def wrapped(self, other):
        assert self.crs == other.crs
        return bool(method(self._geom, other._geom))  # pylint: disable=protected-access
    return wrapped


def _wrap_binary_geom(method):
    @functools.wraps(method, assigned=('__doc__', ))
    def wrapped(self, other):
        assert self.crs == other.crs
        return _make_geom_from_ogr(method(self._geom, other._geom), self.crs)  # pylint: disable=protected-access
    return wrapped


class Geometry(object):
    """
    2D Geometry with CRS

    Instantiate with a GeoJSON structure

    If 3D coordinates are supplied, they are converted to 2D by dropping the Z points.

    :type _geom: ogr.Geometry
    :type crs: CRS
    """
    _geom_makers = {
        'Point': _make_point,
        'MultiPoint': _make_multipoint,
        'LineString': _make_line,
        'MultiLineString': _make_multiline,
        'Polygon': _make_polygon,
        'MultiPolygon': _make_multipolygon,
    }

    _geom_types = {
        ogr.wkbPoint: 'Point',
        ogr.wkbMultiPoint: 'MultiPoint',
        ogr.wkbLineString: 'LineString',
        ogr.wkbMultiLineString: 'MultiLineString',
        ogr.wkbPolygon: 'Polygon',
        ogr.wkbMultiPolygon: 'MultiPolygon',
    }

    contains = _wrap_binary_bool(ogr.Geometry.Contains)
    crosses = _wrap_binary_bool(ogr.Geometry.Crosses)
    disjoint = _wrap_binary_bool(ogr.Geometry.Disjoint)
    intersects = _wrap_binary_bool(ogr.Geometry.Intersects)
    touches = _wrap_binary_bool(ogr.Geometry.Touches)
    within = _wrap_binary_bool(ogr.Geometry.Within)
    overlaps = _wrap_binary_bool(ogr.Geometry.Overlaps)

    difference = _wrap_binary_geom(ogr.Geometry.Difference)
    intersection = _wrap_binary_geom(ogr.Geometry.Intersection)
    symmetric_difference = _wrap_binary_geom(ogr.Geometry.SymDifference)
    union = _wrap_binary_geom(ogr.Geometry.Union)

    def __init__(self, geo, crs=None):
        self.crs = crs
        self._geom = Geometry._geom_makers[geo['type']](geo['coordinates'])

    @property
    def type(self):
        return Geometry._geom_types[self._geom.GetGeometryType()]

    @property
    def is_empty(self):
        return self._geom.IsEmpty()

    @property
    def is_valid(self):
        return self._geom.IsValid()

    @property
    def boundary(self):
        return _make_geom_from_ogr(self._geom.Boundary(), self.crs)

    @property
    def centroid(self):
        return _make_geom_from_ogr(self._geom.Centroid(), self.crs)

    @property
    def coords(self):
        return self._geom.GetPoints()

    @property
    def points(self):
        return self.coords

    @property
    def length(self):
        return self._geom.Length()

    @property
    def area(self):
        return self._geom.GetArea()

    @property
    def convex_hull(self):
        return _make_geom_from_ogr(self._geom.ConvexHull(), self.crs)

    @property
    def envelope(self):
        minx, maxx, miny, maxy = self._geom.GetEnvelope()
        return BoundingBox(left=minx, right=maxx, bottom=miny, top=maxy)

    @property
    def boundingbox(self):
        return self.envelope

    @property
    def wkt(self):
        return getattr(self._geom, 'ExportToIsoWkt', self._geom.ExportToWkt)()

    @property
    def json(self):
        return self.__geo_interface__

    @property
    def __geo_interface__(self):
        return {
            'type': self.type,
            'coordinates': _get_coordinates(self._geom)
        }

    def segmented(self, resolution):
        """
        Possibly add more points to the geometry so that no edge is longer than `resolution`
        """
        clone = self._geom.Clone()
        clone.Segmentize(resolution)
        # Segmentize can cause issues with polygons using GDAL 2.4.1
        # See: https://github.com/OSGeo/gdal/issues/1414
        clone.CloseRings()
        return _make_geom_from_ogr(clone, self.crs)

    def interpolate(self, distance):
        """
        Returns a point distance units along the line or None if underlying
        geometry doesn't support this operation.
        """
        geom = self._geom.Value(distance)
        if geom is None:
            return None
        return _make_geom_from_ogr(geom, self.crs)

    def buffer(self, distance, quadsecs=30):
        return _make_geom_from_ogr(self._geom.Buffer(distance, quadsecs), self.crs)

    def simplify(self, tolerance):
        return _make_geom_from_ogr(self._geom.Simplify(tolerance), self.crs)

    def to_crs(self, crs, resolution=None, wrapdateline=False):
        """
        Convert geometry to a different Coordinate Reference System

        :param CRS crs: CRS to convert to
        :param float resolution: Subdivide the geometry such it has no segment longer then the given distance.
        :param bool wrapdateline: Attempt to gracefully handle geometry that intersects the dateline
                                  when converting to geographic projections.
                                  Currently only works in few specific cases (source CRS is smooth over the dateline).
        :rtype: Geometry
        """
        if self.crs == crs:
            return self

        if resolution is None:
            resolution = 1 if self.crs.geographic else 100000

        transform = mk_osr_point_transform(self.crs, crs)
        clone = self._geom.Clone()

        if wrapdateline and crs.geographic:
            rtransform = mk_osr_point_transform(crs, self.crs)
            clone = _chop_along_antimeridian(clone, transform, rtransform)

        clone.Segmentize(resolution)
        # Segmentize can cause issues with polygons using GDAL 2.4.1
        # See: https://github.com/OSGeo/gdal/issues/1414
        clone.CloseRings()
        clone.Transform(transform)

        return _make_geom_from_ogr(clone, crs)  # pylint: disable=protected-access

    def __iter__(self):
        for i in range(self._geom.GetGeometryCount()):
            yield _make_geom_from_ogr(self._geom.GetGeometryRef(i), self.crs)

    def __nonzero__(self):
        return not self.is_empty

    def __bool__(self):
        return not self.is_empty

    def __eq__(self, other):
        return (hasattr(other, 'crs') and self.crs == other.crs and
                hasattr(other, '_geom') and self._geom.Equal(other._geom))  # pylint: disable=protected-access

    def __str__(self):
        return 'Geometry(%s, %r)' % (self.__geo_interface__, self.crs)

    def __repr__(self):
        return 'Geometry(%s, %s)' % (self._geom, self.crs)

    # Implement pickle/unpickle
    # It does work without these two methods, but gdal/ogr prints 'ERROR 1: Empty geometries cannot be constructed'
    # when unpickling, which is quite unpleasant.
    def __getstate__(self):
        return {'geo': self.json, 'crs': self.crs}

    def __setstate__(self, state):
        self.__init__(**state)


def _dist(x, y):
    return x*x + y*y


def _chop_along_antimeridian(geom, transform, rtransform):
    """
    attempt to cut the geometry along the dateline
    idea borrowed from TransformBeforeAntimeridianToWGS84 with minor mods...
    """
    minx, maxx, miny, maxy = geom.GetEnvelope()

    midx, midy = (minx+maxx)/2, (miny+maxy)/2
    mid_lon, mid_lat, _ = transform.TransformPoint(midx, midy)

    eps = 1.0e-9
    if not _is_smooth_across_dateline(mid_lat, transform, rtransform, eps):
        return geom

    left_of_dt = _make_line([(180 - eps, -90), (180 - eps, 90)])
    left_of_dt.Segmentize(1)
    # Segmentize can cause issues with polygons using GDAL 2.4.1
    # See: https://github.com/OSGeo/gdal/issues/1414
    left_of_dt.CloseRings()
    left_of_dt.Transform(rtransform)

    if not left_of_dt.Intersects(geom):
        return geom

    right_of_dt = _make_line([(-180 + eps, -90), (-180 + eps, 90)])
    right_of_dt.Segmentize(1)
    # Segmentize can cause issues with polygons using GDAL 2.4.1
    # See: https://github.com/OSGeo/gdal/issues/1414
    right_of_dt.CloseRings()
    right_of_dt.Transform(rtransform)

    chopper = _make_multipolygon([[[(minx, maxy), (minx, miny)] + left_of_dt.GetPoints() + [(minx, maxy)]],
                                  [[(maxx, maxy), (maxx, miny)] + right_of_dt.GetPoints() + [(maxx, maxy)]]])
    return geom.Intersection(chopper)


def _is_smooth_across_dateline(mid_lat, transform, rtransform, eps):
    """
    test whether the CRS is smooth over the dateline
    idea borrowed from IsAntimeridianProjToWGS84 with minor mods...
    """
    left_of_dt_x, left_of_dt_y, _ = rtransform.TransformPoint(180-eps, mid_lat)
    right_of_dt_x, right_of_dt_y, _ = rtransform.TransformPoint(-180+eps, mid_lat)

    if _dist(right_of_dt_x-left_of_dt_x, right_of_dt_y-left_of_dt_y) > 1:
        return False

    left_of_dt_lon, left_of_dt_lat, _ = transform.TransformPoint(left_of_dt_x, left_of_dt_y)
    right_of_dt_lon, right_of_dt_lat, _ = transform.TransformPoint(right_of_dt_x, right_of_dt_y)
    if (_dist(left_of_dt_lon - 180 + eps, left_of_dt_lat - mid_lat) > 2 * eps or
            _dist(right_of_dt_lon + 180 - eps, right_of_dt_lat - mid_lat) > 2 * eps):
        return False

    return True


###########################################
# Helper constructor functions a la shapely
###########################################


def point(x, y, crs):
    """
    Create a 2D Point

    >>> point(10, 10, crs=None)
    Geometry(POINT (10 10), None)

    :rtype: Geometry
    """
    return Geometry({'type': 'Point', 'coordinates': (x, y)}, crs=crs)


def multipoint(coords, crs):
    """
    Create a 2D MultiPoint Geometry

    >>> multipoint([(10, 10), (20, 20)], None)
    Geometry(MULTIPOINT (10 10,20 20), None)

    :param list coords: list of x,y coordinate tuples
    :rtype: Geometry
    """
    return Geometry({'type': 'MultiPoint', 'coordinates': coords}, crs=crs)


def line(coords, crs):
    """
    Create a 2D LineString (Connected set of lines)

    >>> line([(10, 10), (20, 20), (30, 40)], None)
    Geometry(LINESTRING (10 10,20 20,30 40), None)

    :param list coords: list of x,y coordinate tuples
    :rtype: Geometry
    """
    return Geometry({'type': 'LineString', 'coordinates': coords}, crs=crs)


def multiline(coords, crs):
    """
    Create a 2D MultiLineString (Multiple disconnected sets of lines)

    >>> multiline([[(10, 10), (20, 20), (30, 40)], [(50, 60), (70, 80), (90, 99)]], None)
    Geometry(MULTILINESTRING ((10 10,20 20,30 40),(50 60,70 80,90 99)), None)

    :param list coords: list of lists of x,y coordinate tuples
    :rtype: Geometry
    """
    return Geometry({'type': 'MultiLineString', 'coordinates': coords}, crs=crs)


def polygon(outer, crs, *inners):
    """
    Create a 2D Polygon

    >>> polygon([(10, 10), (20, 20), (20, 10), (10, 10)], None)
    Geometry(POLYGON ((10 10,20 20,20 10,10 10)), None)

    :param list coords: list of 2d x,y coordinate tuples
    :rtype: Geometry
    """
    return Geometry({'type': 'Polygon', 'coordinates': (outer, )+inners}, crs=crs)


def multipolygon(coords, crs):
    """
    Create a 2D MultiPolygon

    >>> multipolygon([[[(10, 10), (20, 20), (20, 10), (10, 10)]], [[(40, 10), (50, 20), (50, 10), (40, 10)]]], None)
    Geometry(MULTIPOLYGON (((10 10,20 20,20 10,10 10)),((40 10,50 20,50 10,40 10))), None)

    :param list coords: list of lists of x,y coordinate tuples
    :rtype: Geometry
    """
    return Geometry({'type': 'MultiPolygon', 'coordinates': coords}, crs=crs)


def box(left, bottom, right, top, crs):
    """
    Create a 2D Box (Polygon)

    >>> box(10, 10, 20, 20, None)
    Geometry(POLYGON ((10 10,10 20,20 20,20 10,10 10)), None)
    """
    points = [(left, bottom), (left, top), (right, top), (right, bottom), (left, bottom)]
    return polygon(points, crs=crs)


def polygon_from_transform(width, height, transform, crs):
    """
    Create a 2D Polygon from an affine transform

    :param float width:
    :param float height:
    :param Affine transform:
    :param crs: CRS
    :rtype:  Geometry
    """
    points = [(0, 0), (0, height), (width, height), (width, 0), (0, 0)]
    transform.itransform(points)
    return polygon(points, crs=crs)


###########################################
# Multi-geometry operations
###########################################


def unary_union(geoms):
    """
    compute union of multiple (multi)polygons efficiently
    """
    # pylint: disable=protected-access
    geom = ogr.Geometry(ogr.wkbMultiPolygon)
    crs = None
    for g in geoms:
        if crs:
            assert crs == g.crs
        else:
            crs = g.crs
        if g._geom.GetGeometryType() == ogr.wkbPolygon:
            geom.AddGeometry(g._geom)
        elif g._geom.GetGeometryType() == ogr.wkbMultiPolygon:
            for poly in g._geom:
                geom.AddGeometry(poly)
        else:
            raise ValueError('"%s" is not supported' % g.type)
    union = geom.UnionCascaded()
    return _make_geom_from_ogr(union, crs)


def unary_intersection(geoms):
    """
    compute intersection of multiple (multi)polygons
    """
    return functools.reduce(Geometry.intersection, geoms)


def _align_pix(left, right, res, off):
    """
    >>> "%.2f %d" % _align_pix(20, 30, 10, 0)
    '20.00 1'
    >>> "%.2f %d" % _align_pix(20, 30.5, 10, 0)
    '20.00 1'
    >>> "%.2f %d" % _align_pix(20, 31.5, 10, 0)
    '20.00 2'
    >>> "%.2f %d" % _align_pix(20, 30, 10, 3)
    '13.00 2'
    >>> "%.2f %d" % _align_pix(20, 30, 10, -3)
    '17.00 2'
    >>> "%.2f %d" % _align_pix(20, 30, -10, 0)
    '30.00 1'
    >>> "%.2f %d" % _align_pix(19.5, 30, -10, 0)
    '30.00 1'
    >>> "%.2f %d" % _align_pix(18.5, 30, -10, 0)
    '30.00 2'
    >>> "%.2f %d" % _align_pix(20, 30, -10, 3)
    '33.00 2'
    >>> "%.2f %d" % _align_pix(20, 30, -10, -3)
    '37.00 2'
    """
    if res < 0:
        res = -res
        val = math.ceil((right - off) / res) * res + off
        width = max(1, int(math.ceil((val - left - 0.1 * res) / res)))
    else:
        val = math.floor((left - off) / res) * res + off
        width = max(1, int(math.ceil((right - val - 0.1 * res) / res)))
    return val, width


class GeoBox(object):
    """
    Defines the location and resolution of a rectangular grid of data,
    including it's :py:class:`CRS`.

    :param geometry.CRS crs: Coordinate Reference System
    :param affine.Affine affine: Affine transformation defining the location of the geobox
    """

    def __init__(self, width, height, affine, crs):
        assert is_affine_st(affine), "Only axis-aligned geoboxes are currently supported"
        #: :type: int
        self.width = width
        #: :type: int
        self.height = height
        #: :rtype: affine.Affine
        self.affine = affine
        #: :rtype: geometry.Geometry
        self.extent = polygon_from_transform(width, height, affine, crs=crs)

    @classmethod
    def from_geopolygon(cls, geopolygon, resolution, crs=None, align=None):
        """
        :type geopolygon: geometry.Geometry
        :param resolution: (y_resolution, x_resolution)
        :param geometry.CRS crs: CRS to use, if different from the geopolygon
        :param (float,float) align: Align geobox such that point 'align' lies on the pixel boundary.
        :rtype: GeoBox
        """
        align = align or (0.0, 0.0)
        assert 0.0 <= align[1] <= abs(resolution[1]), "X align must be in [0, abs(x_resolution)] range"
        assert 0.0 <= align[0] <= abs(resolution[0]), "Y align must be in [0, abs(y_resolution)] range"

        if crs is None:
            crs = geopolygon.crs
        else:
            geopolygon = geopolygon.to_crs(crs)

        bounding_box = geopolygon.boundingbox
        offx, width = _align_pix(bounding_box.left, bounding_box.right, resolution[1], align[1])
        offy, height = _align_pix(bounding_box.bottom, bounding_box.top, resolution[0], align[0])
        affine = (Affine.translation(offx, offy) * Affine.scale(resolution[1], resolution[0]))
        return GeoBox(crs=crs, affine=affine, width=width, height=height)

    def buffered(self, ybuff, xbuff):
        """
        Produce a tile buffered by ybuff, xbuff (in CRS units)
        """
        by, bx = (_round_to_res(buf, res) for buf, res in zip((ybuff, xbuff), self.resolution))
        affine = self.affine * Affine.translation(-bx, -by)

        return GeoBox(width=self.width + 2*bx,
                      height=self.height + 2*by,
                      affine=affine,
                      crs=self.crs)

    def __getitem__(self, roi):
        if isinstance(roi, int):
            roi = (slice(roi, roi+1), slice(None, None))

        if isinstance(roi, slice):
            roi = (roi, slice(None, None))

        if len(roi) > 2:
            raise ValueError('Expect 2d slice')

        if not all(s.step is None or s.step == 1 for s in roi):
            raise NotImplementedError('scaling not implemented, yet')

        roi = roi_normalise(roi, self.shape)
        ty, tx = [s.start for s in roi]
        h, w = roi_shape(roi)

        affine = self.affine * Affine.translation(tx, ty)

        return GeoBox(width=w, height=h, affine=affine, crs=self.crs)

    def __or__(self, other):
        """ A geobox that encompasses both self and other. """
        return geobox_union_conservative([self, other])

    def __and__(self, other):
        """ A geobox that is contained in both self and other. """
        return geobox_intersection_conservative([self, other])

    def is_empty(self):
        return self.width == 0 or self.height == 0

    def __bool__(self):
        return not self.is_empty()

    @property
    def transform(self):
        return self.affine

    @property
    def shape(self):
        """
        :type: (int,int)
        """
        return self.height, self.width

    @property
    def crs(self):
        """
        :rtype: CRS
        """
        return self.extent.crs

    @property
    def dimensions(self):
        """
        List of dimension names of the GeoBox

        :type: (str,str)
        """
        return self.crs.dimensions

    @property
    def resolution(self):
        """
        Resolution in Y,X dimensions

        :type: (float,float)
        """
        return self.affine.e, self.affine.a

    @property
    def alignment(self):
        """
        Alignment of pixel boundaries in Y,X dimensions

        :type: (float,float)
        """
        return self.affine.yoff % abs(self.affine.e), self.affine.xoff % abs(self.affine.a)

    @property
    def coordinates(self):
        """
        dict of coordinate labels

        :type: dict[str,numpy.array]
        """
        yres, xres = self.resolution
        yoff, xoff = self.affine.yoff, self.affine.xoff

        xs = numpy.arange(self.width) * xres + (xoff + xres / 2)
        ys = numpy.arange(self.height) * yres + (yoff + yres / 2)

        crs = self.crs

        return OrderedDict((dim, Coordinate(labels, units, res))
                           for dim, labels, units, res in zip(crs.dimensions, (ys, xs), crs.units, (yres, xres)))

    @property
    def geographic_extent(self):
        """
        :rtype: geometry.Geometry
        """
        if self.crs.geographic:
            return self.extent
        return self.extent.to_crs(CRS('EPSG:4326'))

    coords = coordinates
    dims = dimensions

    def __str__(self):
        return "GeoBox({})".format(self.geographic_extent)

    def __repr__(self):
        return "GeoBox({width}, {height}, {affine!r}, {crs})".format(
            width=self.width,
            height=self.height,
            affine=self.affine,
            crs=self.extent.crs
        )

    def __eq__(self, other):
        if not isinstance(other, GeoBox):
            return False

        return (self.shape == other.shape
                and self.transform == other.transform
                and self.crs == other.crs)


def bounding_box_in_pixel_domain(geobox: GeoBox, reference: GeoBox) -> BoundingBox:
    """
    Returns the bounding box of `geobox` with respect to the pixel grid
    defined by `reference` when their coordinate grids are compatible,
    that is, have the same CRS, same pixel size and orientation, and
    are related by whole pixel translation,
    otherwise raises `ValueError`.
    """
    tol = 1.e-8

    if reference.crs != geobox.crs:
        raise ValueError("Cannot combine geoboxes in different CRSs")

    a, b, c, d, e, f, *_ = ~reference.affine * geobox.affine

    if not (numpy.isclose(a, 1) and numpy.isclose(b, 0) and is_almost_int(c, tol)
            and numpy.isclose(d, 0) and numpy.isclose(e, 1) and is_almost_int(f, tol)):
        raise ValueError("Incompatible grids")

    tx, ty = round(c), round(f)
    return BoundingBox(tx, ty, tx + geobox.width, ty + geobox.height)


def geobox_union_conservative(geoboxes: List[GeoBox]) -> GeoBox:
    """ Union of geoboxes. Fails whenever incompatible grids are encountered. """
    if len(geoboxes) == 0:
        raise ValueError("No geoboxes supplied")

    reference, *_ = geoboxes

    bbox = bbox_union(bounding_box_in_pixel_domain(geobox, reference=reference)
                      for geobox in geoboxes)

    affine = reference.affine * Affine.translation(*bbox[:2])

    return GeoBox(width=bbox.width, height=bbox.height, affine=affine, crs=reference.crs)


def geobox_intersection_conservative(geoboxes: List[GeoBox]) -> GeoBox:
    """
    Intersection of geoboxes. Fails whenever incompatible grids are encountered.
    """
    if len(geoboxes) == 0:
        raise ValueError("No geoboxes supplied")

    reference, *_ = geoboxes

    bbox = bbox_intersection(bounding_box_in_pixel_domain(geobox, reference=reference)
                             for geobox in geoboxes)

    # standardise empty geobox representation
    if bbox.left > bbox.right:
        bbox = BoundingBox(left=bbox.left, bottom=bbox.bottom, right=bbox.left, top=bbox.top)
    if bbox.bottom > bbox.top:
        bbox = BoundingBox(left=bbox.left, bottom=bbox.bottom, right=bbox.right, top=bbox.bottom)

    affine = reference.affine * Affine.translation(*bbox[:2])

    return GeoBox(width=bbox.width, height=bbox.height, affine=affine, crs=reference.crs)


def scaled_down_geobox(src_geobox, scaler: int):
    """Given a source geobox and integer scaler compute geobox of a scaled down image.

        Output geobox will be padded when shape is not a multiple of scaler.
        Example: 5x4, scaler=2 -> 3x2

        NOTE: here we assume that pixel coordinates are 0,0 at the top-left
              corner of a top-left pixel.

    """
    assert scaler > 1

    H, W = [X//scaler + (1 if X % scaler else 0)
            for X in src_geobox.shape]

    # Since 0,0 is at the corner of a pixel, not center, there is no
    # translation between pixel plane coords due to scaling
    A = src_geobox.transform * Affine.scale(scaler, scaler)

    return GeoBox(W, H, A, src_geobox.crs)


def _round_to_res(value, res, acc=0.1):
    """
    >>> _round_to_res(0.2, 1.0)
    1
    >>> _round_to_res(0.0, 1.0)
    0
    >>> _round_to_res(0.05, 1.0)
    0
    """
    res = abs(res)
    return int(math.ceil((value - 0.1 * res) / res))


def intersects(a, b):
    return a.intersects(b) and not a.touches(b)


def bbox_union(bbs: Iterable[BoundingBox]) -> BoundingBox:
    """ Given a stream of bounding boxes compute enclosing BoundingBox
    """
    # pylint: disable=invalid-name

    L = B = float('+inf')
    R = T = float('-inf')

    for bb in bbs:
        l, b, r, t = bb
        L = min(l, L)
        B = min(b, B)
        R = max(r, R)
        T = max(t, T)

    return BoundingBox(L, B, R, T)


def bbox_intersection(bbs: Iterable[BoundingBox]) -> BoundingBox:
    """ Given a stream of bounding boxes compute the overlap BoundingBox
    """
    # pylint: disable=invalid-name

    L = B = float('-inf')
    R = T = float('+inf')

    for bb in bbs:
        l, b, r, t = bb
        L = max(l, L)
        B = max(b, B)
        R = min(r, R)
        T = min(t, T)

    return BoundingBox(L, B, R, T)
