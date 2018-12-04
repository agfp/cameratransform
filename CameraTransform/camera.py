import numpy as np
import pandas as pd
import os
import json
import itertools
import matplotlib.pyplot as plt
import cv2
from scipy.optimize import minimize
from .parameter_set import ParameterSet, ClassWithParameterSet, Parameter, TYPE_GPS
from .projection import RectilinearProjection, CameraProjection
from .spatial import SpatialOrientation
from .lens_distortion import NoDistortion, LensDistortion
from . import gps
from . import ray


def _getSensorFromDatabase(model):
    """
    Get the sensor size from the given model from the database at: https://github.com/openMVG/CameraSensorSizeDatabase

    Parameters
    ----------
    model: string
        the model name as received from the exif data

    Returns
    -------
    sensor_size: tuple
        (sensor_width, sensor_height) in mm or None
    """
    import requests

    url = "https://raw.githubusercontent.com/openMVG/CameraSensorSizeDatabase/master/sensor_database_detailed.csv"

    database_filename = "sensor_database_detailed.csv"
    # download the database if it is not there
    if not os.path.exists(database_filename):
        with open(database_filename, "w") as fp:
            print("Downloading database from:", url)
            r = requests.get(url)
            fp.write(r.text)
    # load the database
    with open(database_filename, "r") as fp:
        data = fp.readlines()

    # format the name
    model = model.replace(" ", ";", 1)
    name = model + ";"
    # try to find it
    for line in data:
        if line.startswith(name):
            # extract the sensor dimensions
            line = line.split(";")
            sensor_size = (float(line[3]), float(line[4]))
            return sensor_size
    # no sensor size found
    return None


def getCameraParametersFromExif(filename, verbose=False, sensor_from_database=True):
    """
    Try to extract the intrinsic camera parameters from the exif information.

    Parameters
    ----------
    filename: basestring
        the filename of the image to load.
    verbose: bool
         whether to print the output.
    sensor_from_database: bool
        whether to try to load the sensor size from a database at https://github.com/openMVG/CameraSensorSizeDatabase

    Returns
    -------
    focal_length: number
        the extracted focal length in mm
    sensor_size: tuple
        (width, height) of the camera sensor in mm
    image_size: tuple
        (width, height) of the image in pixel

    Examples
    --------

    >>> import CameraTransform as ct

    Supply the image filename to print the results:

    >>> ct.getCameraParametersFromExif("Image.jpg", verbose=True)
    Intrinsic parameters for 'Canon EOS 50D':
       focal length: 400.0 mm
       sensor size: 22.3 mm × 14.9 mm
       image size: 4752 × 3168 Pixels

    Or use the resulting parameters to initialize a CameraTransform instance:

    >>> focal_length, sensor_size, image_size = ct.getCameraParametersFromExif("Image.jpg")
    >>> cam = ct.Camera(focal_length, sensor=sensor_size, image=image_size)

    """
    from PIL import Image
    from PIL.ExifTags import TAGS

    def get_exif(fn):
        ret = {}
        i = Image.open(fn)
        info = i._getexif()
        for tag, value in info.items():
            decoded = TAGS.get(tag, tag)
            ret[decoded] = value
        return ret

    # read the exif information of the file
    exif = get_exif(filename)
    # get the focal length
    f = exif["FocalLength"][0] / exif["FocalLength"][1]
    # get the sensor size, either from a database
    if sensor_from_database:
        sensor_size = _getSensorFromDatabase(exif["Model"])
    # or from the exif information
    if not sensor_size or sensor_size is None:
        sensor_size = (
            exif["ExifImageWidth"] / (exif["FocalPlaneXResolution"][0] / exif["FocalPlaneXResolution"][1]) * 25.4,
            exif["ExifImageHeight"] / (exif["FocalPlaneYResolution"][0] / exif["FocalPlaneYResolution"][1]) * 25.4)
    # get the image size
    image_size = (exif["ExifImageWidth"], exif["ExifImageHeight"])
    # print the output if desired
    if verbose:
        print("Intrinsic parameters for '%s':" % exif["Model"])
        print("   focal length: %.1f mm" % f)
        print("   sensor size: %.1f mm × %.1f mm" % sensor_size)
        print("   image size: %d × %d Pixels" % image_size)
    return f, sensor_size, image_size


class CameraGroup(ClassWithParameterSet):
    projection_list = None
    orientation_list = None
    lens_list = None

    def __init__(self, projection, orientation=None, lens=None):
        self.N = 1

        def checkCount(parameter, class_type, parameter_name, default):
            if parameter is None:
                setattr(self, parameter_name, [default()])
            elif isinstance(parameter, class_type):
                setattr(self, parameter_name, [parameter])
            else:
                setattr(self, parameter_name, list(parameter))
                self.N = len(getattr(self, parameter_name))

        checkCount(projection, CameraProjection, "projection_list", RectilinearProjection)
        checkCount(orientation, SpatialOrientation, "orientation_list", SpatialOrientation)
        checkCount(lens, LensDistortion, "lens_list", NoDistortion)

        params = {}
        def gatherParameters(parameter_list):
            if len(parameter_list) == 1:
                params.update(parameter_list[0].parameters.parameters)
            else:
                for index, proj in enumerate(parameter_list):
                    for name in proj.parameters.parameters:
                        params["C%d_%s" % (index, name)] = proj.parameters.parameters[name]

        gatherParameters(self.projection_list)
        gatherParameters(self.orientation_list)
        gatherParameters(self.lens_list)

        self.parameters = ParameterSet(**params)

        self.cameras = [Camera(projection, orientation, lens) for index, projection, orientation, lens in
                        zip(range(self.N), itertools.cycle(self.projection_list), itertools.cycle(self.orientation_list), itertools.cycle(self.lens_list))]

    def getBaseline(self):
        return np.sqrt((self[0].pos_x_m-self[1].pos_x_m)**2 + (self[0].pos_y_m-self[1].pos_y_m)**2)

    def fit(self, cost_function, param_type=None):
        names = self.parameters.get_fit_parameters(param_type)
        ranges = self.parameters.get_parameter_ranges(names)
        estimates = self.parameters.get_parameter_defaults(names)

        def cost(p):
            self.parameters.set_fit_parameters(names, p)
            return cost_function()

        p = minimize(cost, estimates, bounds=ranges)
        self.parameters.set_fit_parameters(names, p["x"])
        return p

    def sampled_fit(self, cost_function, sample_function, N=1000, param_type=None):
        names = self.parameters.get_fit_parameters(param_type)
        fits = []
        for i in range(N):
            print("Sample", i)
            sample_function()
            r = self.fit(cost_function)
            fits.append(r["x"])
        fit_data = np.array(fits)
        for i, name in enumerate(names):
            data = fit_data[:, i]
            self.parameters.parameters[name].set_stats(np.mean(data), np.std(data))
        return fit_data

    def spaceFromImages(self, points1, points2):
        p1, v1 = self.cameras[0].getRay(points1)
        p2, v2 = self.cameras[1].getRay(points2)
        return ray.intersectionOfTwoLines(p1, v1, p2, v2)

    def discanteBetweenRays(self, points1, points2):
        p1, v1 = self.cameras[0].getRay(points1, normed=True)
        p2, v2 = self.cameras[1].getRay(points2, normed=True)
        return ray.distanceOfTwoLines(p1, v1, p2, v2)

    def imagesFromSpace(self, points):
        return [cam.imageFromSpace(points) for cam in self.cameras]

    def __getitem__(self, item):
        return self.cameras[item]

    def __len__(self):
        return len(self.cameras)

    def __iter__(self):
        return iter(self.cameras)


class Camera(ClassWithParameterSet):
    """
    This class is the core of the CameraTransform package and represents a camera. Each camera has a projection
    (subclass of :py:class:`CameraProjection`), a spatial orientation (:py:class:`SpatialOrientation`) and optionally
    a lens distortion (subclass of :py:class:`LensDistortion`).
    """
    map = None
    last_extent = None
    last_scaling = None

    map_undistort = None
    last_extent_undistort = None
    last_scaling_undistort = None

    fit_method = None

    R_earth = 6371e3

    def __init__(self, projection, orientation=None, lens=None):
        self.projection = projection
        if orientation is None:
            orientation = SpatialOrientation()
        self.orientation = orientation
        if lens is None:
            lens = NoDistortion()
        self.lens = lens
        self.lens.setProjection(projection)

        params = dict(gps_lat=Parameter(0, default=0, type=TYPE_GPS), gps_lon=Parameter(0, default=0, type=TYPE_GPS))
        params.update(self.projection.parameters.parameters)
        params.update(self.orientation.parameters.parameters)
        params.update(self.lens.parameters.parameters)
        self.parameters = ParameterSet(**params)

    def __str__(self):
        string = "CameraTransform(\n"
        string += str(self.lens)
        string += str(self.projection)
        string += str(self.orientation)
        string += ")"
        return string

    def setGPSpos(self, lat, lon=None, elevation=None):
        """
        Provide the earth position for the camera.

        Parameters
        ----------
        lat: number, string
            the latitude of the camera or the string representing the gps position.
        lon: number, optional
            the longitude of the camera.
        elevation: number, optional
            the elevation of the camera.

        Examples
        --------

        >>> import CameraTransform as ct
        >>> cam = ct.Camera()

        Supply the gps position of the camera as floats:

        >>> cam.setGPSpos(-66.66, 140.00, 19)

        or as a string:

        >>> cam.setGPSpos("66°39'53.4\"S	140°00'34.8\"")
        """
        # if it is a string
        if isinstance(lat, str):
            lat, lon, elevation = gps.gpsFromString(lat, height=elevation)
        else:
            # if it is a tuple
            try:
                lat, lon, elevation = gps.splitGPS(lat)
            # or if it is just a single value
            except AttributeError:
                pass
        self.gps_lat = lat
        self.gps_lon = lon
        if elevation is not None:
            self.elevation_m = elevation

    def fit(self, cost_function, param_type=None):
        names = self.parameters.get_fit_parameters(param_type)
        ranges = self.parameters.get_parameter_ranges(names)
        estimates = self.parameters.get_parameter_defaults(names)

        def cost(p):
            self.parameters.set_fit_parameters(names, p)
            return cost_function()

        p = minimize(cost, estimates, bounds=ranges, method=self.fit_method)
        self.parameters.set_fit_parameters(names, p["x"])
        return p

    def sampled_fit(self, cost_function, sample_function, N=1000, param_type=None):
        names = self.parameters.get_fit_parameters(param_type)
        fits = []
        for i in range(N):
            print("Sample", i)
            sample_function()
            r = self.fit(cost_function)
            fits.append(r["x"])
        fit_data = np.array(fits)
        for i, name in enumerate(names):
            data = fit_data[:, i]
            self.parameters.parameters[name].set_stats(np.mean(data), np.std(data))
        return fit_data

    def fitCamParametersFromObjects(self, points_foot, points_head, object_height=1, object_elevation=0, points_horizon=None):
        """
        Fit the camera parameters for given objects of equal heights. The foot positions are given in points_foot and
        the heads are given in points_head. The height of each objects is given in object_height, and if the objects are
        not at sea level, an object_elevation can be given.

        For an example see: `Fit from object heights <fit_heights.html>`_

        Parameters
        ----------
        points_foot: ndarray
            The pixel positions of the feet of the objects in the image.
        points_head: ndarray
            The pixel positions of the heads of the objects in the image.
        object_height: number, optional
            The height of the objects. Default = 1m
        object_elevation: number, optional
            The elevation of the feet ot the objects.

        Returns
        -------
        p: list
            the fitted parameters.
        """
        points_foot = np.array(points_foot)
        points_head = np.array(points_head)
        if points_horizon is not None:
            points_horizon = np.array(points_horizon)

        # the heading and position cannot be fitted using just relative distances
        self.heading_deg = 0
        self.pos_x_m = 0
        self.pos_y_m = 0

        def cost():
            if points_horizon is not None:
                horizon_points_fit = self.getImageHorizon(points_horizon[:, 0])
                #print("horizon_points_fit", horizon_points_fit)
                error_horizon = np.mean(np.linalg.norm(points_horizon - horizon_points_fit, axis=1)**2)
            else:
                error_horizon = 0

            # project the feet from the image to the space with the provided elevation
            estimated_foot_space = self.spaceFromImage(points_foot.copy(), Z=object_elevation)
            # add the object height to the z position
            estimated_foot_space[:, 2] = object_elevation + object_height
            # transform the "head" positions back
            estimated_head_image = self.imageFromSpace(estimated_foot_space)
            # calculate the distance between real pixel position and estimated position
            pixels = np.linalg.norm(points_head - estimated_head_image, axis=1)
            # the error is the squared sum
            #print(np.mean(pixels ** 2), error_horizon*10, self.roll_deg)
            #return error_horizon
            return np.mean(pixels ** 2)+error_horizon

        # fit with the given cost function
        return self.fit(cost)

    def distanceToHorizon(self):
        return np.sqrt(2 * self.R_earth ** 2 * (1 - self.R_earth / (self.R_earth + self.elevation_m)))

    def getImageHorizon(self, pointsX=None):
        """
        This function calculates the position of the horizon in the image sampled at the points x=0, x=im_width/2,
        x=im_width.

        :return: The points im camera image coordinates of the horizon in the format of [2xN].
        """
        d = self.distanceToHorizon()
        if pointsX is None:
            pointsX = [0, self.image_width_px/2, self.image_width_px]
        pointsY = np.arange(0, self.image_height_px)

        points = []
        # for every x-coordinate where we want to determine the horizon
        for x in pointsX:
            # test all y points of the image
            p = np.vstack((np.ones(len(pointsY))*x, pointsY)).T
            # transform them to the space with a fixed distance from the camera (the distance to the horizon)
            # and select the point with the z coordinate closest to 0
            try:
                y = np.nanargmin(np.abs(self.spaceFromImage(p, D=d)[:, 2]))
            except ValueError:
                y = np.nan
            # add the found point to the list
            points.append([x, y])
        return np.array(points)

    def getImageBorder(self, resolution=1):
        w, h = self.projection.parameters.image_width_px, self.projection.parameters.image_height_px
        border = []
        for y in np.arange(0, h, resolution):
            border.append([0, y])
        for x in np.arange(0, w, resolution):
            border.append([x, h])
        for y in np.arange(h, 0, -resolution):
            border.append([w, y])
        for x in np.arange(w, 0, -resolution):
            border.append([x, 0])
        return self.spaceFromImage(border, Z=0)

    def getCameraCone(self):
        w, h = self.projection.parameters.image_width_px, self.projection.parameters.image_height_px
        border = []
        corner_indices = [0]
        for y in range(h):
            border.append([0, y])
        corner_indices.append(len(border))
        for x in range(w):
            border.append([x, h])
        corner_indices.append(len(border))
        for y in np.arange(h, 0, -1):
            border.append([w, y])
        corner_indices.append(len(border))
        for x in np.arange(w, 0, -1):
            border.append([x, 0])
        corner_indices.append(len(border))
        border = list(self.spaceFromImage(border, Z=0))
        origin = self.orientation.spaceFromCamera([0, 0, 0])
        for corner_index in corner_indices:
            border.append([np.nan, np.nan, np.nan])
            border.append(origin)
            border.append(border[corner_index])
        return np.array(border)

    def imageFromSpace(self, points):
        """
        Convert points (Nx3) from the **space** coordinate system to the **image** coordinate system.

        Parameters
        ----------
        points : ndarray
            the points in **space** coordinates to transform, dimensions (3), (Nx3)

        Returns
        -------
        points : ndarray
            the points in the **image** coordinate system, dimensions (2), (Nx2)

        Examples
        --------

        >>> import CameraTransform as ct
        >>> cam = ct.Camera(ct.RectilinearProjection(focallength_px=3729, image=(4608, 2592)),
        >>>                    ct.SpatialOrientation(elevation_m=15.4, tilt_deg=85))

        transform a single point from the space to the image:

        >>> cam.imageFromSpace([-4.17, 45.32, 0.])
        [1969.52 2209.73]

        or multiple points in one go:

        >>> cam.imageFromSpace([[-4.03, 43.96,  0.], [-8.57, 47.91, 0.]]))
        [[1971.05 2246.95]
         [1652.73 2144.53]]
        """
        # ensure that the points are provided as an array
        points = np.array(points)
        # project the points from the space to the camera and from the camera to the image
        return self.lens.distortedFromImage(self.projection.imageFromCamera(self.orientation.cameraFromSpace(points)))

    def getRay(self, points, normed=False):
        """
        As the transformation from the **image** coordinate system to the **space** coordinate system is not unique,
        **image** points can only be uniquely mapped to a ray in **space** coordinates.

        Parameters
        ----------
        points : ndarray
            the points in **image** coordinates for which to get the ray, dimensions (2), (Nx2)

        Returns
        -------
        offset : ndarray
            the origin of the camera (= starting point of the rays) in **space** coordinates, dimensions (3)
        rays : ndarray
            the rays in the **space** coordinate system, dimensions (3), (Nx3)

        Examples
        --------

        >>> import CameraTransform as ct
        >>> cam = ct.Camera(ct.RectilinearProjection(focallength_px=3729, image=(4608, 2592)),
        >>>                    ct.SpatialOrientation(elevation_m=15.4, tilt_deg=85))

        get the ray of a point in the image:

        >>> offset, ray = cam.getRay([1968, 2291]))
        >>> offset
        [0.00 0.00 15.40]
        >>> ray
        [-0.09 0.97 -0.35]

        or the rays of multiple points in the image:

        >>> offset, ray, cam.getRay([[1968, 2291], [1650, 2189]])
        >>> offset
        [0.00 0.00 15.40]
        >>> ray
        [[-0.09 0.97 -0.35]
         [-0.18 0.98 -0.33]]
        """
        # ensure that the points are provided as an array
        points = np.array(points)
        # get the camera position in space (the origin of the camera coordinate system)
        offset = self.orientation.spaceFromCamera([0, 0, 0])
        # get the direction fo the ray from the points
        # the projection provides the ray in camera coordinates, which we convert to the space coordinates
        direction = self.orientation.spaceFromCamera(self.projection.getRay(self.lens.imageFromDistorted(points), normed=normed), direction=True)
        # return the offset point and the direction of the ray
        return offset, direction

    def spaceFromImage(self, points, X=None, Y=None, Z=0, D=None, mesh=None):
        """
        Convert points (Nx2) from the **image** coordinate system to the **space** coordinate system. This is not a unique
        transformation, therefore an additional constraint has to be provided. The X, Y, or Z coordinate(s) of the target
        points can be provided or the distance D from the camera.

        Parameters
        ----------
        points : ndarray
            the points in **image** coordinates to transform, dimensions (2), (Nx2)
        X : number, ndarray, optional
            the X coordinate in **space** coordinates of the target points, dimensions scalar, (N)
        Y : number, ndarray, optional
            the Y coordinate in **space** coordinates of the target points, dimensions scalar, (N)
        Z : number, ndarray, optional
            the Z coordinate in **space** coordinates of the target points, dimensions scalar, (N), default 0
        D : number, ndarray, optional
            the distance in **space** coordinates of the target points from the camera, dimensions scalar, (N)
        mesh : ndarray, optional
            project the image coordinates onto the mesh in **space** coordinates. The mesh is a list of M triangles,
            consisting of three 3D points each. Dimensions, (3x3), (Mx3x3)
        Returns
        -------
        points : ndarray
            the points in the **space** coordinate system, dimensions (3), (Nx3)

        Examples
        --------

        >>> import CameraTransform as ct
        >>> cam = ct.Camera(ct.RectilinearProjection(focallength_px=3729, image=(4608, 2592)),
        >>>                    ct.SpatialOrientation(elevation_m=15.4, tilt_deg=85))

        transform a single point (impliying the condition Z=0):

        >>> cam.spaceFromImage([1968 , 2291])
        [-3.93 42.45 0.00]

        transform multiple points:

        >>> cam.spaceFromImage([[1968 , 2291], [1650, 2189]])
        [[-3.93 42.45 0.00]
         [-8.29 46.11 -0.00]]

        points that cannot be projected on the image, because they are behind the camera (for the RectilinearProjection)
        are returned with nan entries:

        >>> cam.imageFromSpace([-4.17, -10.1, 0.])
        [nan nan]

        specify a y coordinate as for the back projection.

        >>> cam.spaceFromImage([[1968 , 2291], [1650, 2189]], Y=45)
        [[-4.17 45.00 -0.93]
         [-8.09 45.00 0.37]]

        or different y coordinates for each point:

        >>> cam.spaceFromImage([[1968 , 2291], [1650, 2189]], Y=[43, 45])
        [[-3.98 43.00 -0.20]
         [-8.09 45.00 0.37]]
        """
        # ensure that the points are provided as an array
        points = np.array(points)
        # get the index which coordinate to force to the given value
        given = np.array([X, Y, Z])
        if X is not None:
            index = 0
        elif Y is not None:
            index = 1
        elif Z is not None:
            index = 2

        # if a mesh is provided, intersect the rays with the mesh
        if mesh is not None:
            # get the rays from the image points
            offset, direction = self.getRay(points)
            return ray.ray_intersect_triangle(offset, direction, mesh)
        # transform to a given distance
        if D is not None:
            # get the rays from the image points (in this case it has to be normed)
            offset, direction = self.getRay(points, normed=True)
            # the factor is than simple the distance
            factor = D
        else:
            # get the rays from the image points
            offset, direction = self.getRay(points)
            # solve the line equation for the factor (how many times the direction vector needs to be added to the origin point)
            factor = (given[index] - offset[..., index]) / direction[..., index]

        if not isinstance(factor, np.ndarray):
            # if factor is not an array, we don't need to specify the broadcasting
            points = direction * factor + offset
        else:
            # apply the factor to the direction vector plus the offset
            points = direction * factor[:, None] + offset[None, :]
        # ignore points that are behind the camera (e.g. trying to project points above the horizon to the ground)
        points[factor < 0] = np.nan
        return points

    def gpsFromSpace(self, points):
        """
        Convert points (Nx3) from the **space** coordinate system to the **gps** coordinate system.

        Parameters
        ----------
        points : ndarray
            the points in **space** coordinates to transform, dimensions (3), (Nx3)

        Returns
        -------
        points : ndarray
            the points in the **gps** coordinate system, dimensions (3), (Nx3)
        """
        return gps.gpsFromSpace(points, np.array(self.gps_lat, self.gps_lon, self.elevation_m))

    def spaceFromGPS(self, points):
        """
        Convert points (Nx3) from the **gps** coordinate system to the **space** coordinate system.

        Parameters
        ----------
        points : ndarray
            the points in **gps** coordinates to transform, dimensions (3), (Nx3)

        Returns
        -------
        points : ndarray
            the points in the **space** coordinate system, dimensions (3), (Nx3)
        """
        return gps.spaceFromGPS(points, np.array(self.gps_lat, self.gps_lon, self.elevation_m))

    def gpsFromImage(self, points, X=None, Y=None, Z=0, D=None):
        """
        Convert points (Nx2) from the **image** coordinate system to the **gps** coordinate system.

        Parameters
        ----------
        points : ndarray
            the points in **image** coordinates to transform, dimensions (2), (Nx2)

        Returns
        -------
        points : ndarray
            the points in the **gps** coordinate system, dimensions (3), (Nx3)
        """
        return self.gpsFromSpace(self.spaceFromImage(points, X=X, Y=Y, Z=Z, D=D))

    def imageFromGPS(self, points):
        """
        Convert points (Nx3) from the **gps** coordinate system to the **image** coordinate system.

        Parameters
        ----------
        points : ndarray
            the points in **gps** coordinates to transform, dimensions (3), (Nx3)

        Returns
        -------
        points : ndarray
            the points in the **image** coordinate system, dimensions (2), (Nx2)
        """
        return self.imageFromSpace(self.spaceFromGPS(points))

    def getObjectHeight(self, point_feet, point_heads, Z=0):
        # get the feet positions in the world
        point3D_feet = self.spaceFromImage(point_feet, Z=Z)
        # get the head positions in the world
        point3D_head1 = self.spaceFromImage(point_heads, Y=point3D_feet[:, 1])
        point3D_head2 = self.spaceFromImage(point_heads, X=point3D_feet[:, 0])
        point3D_head = np.mean([point3D_head1, point3D_head2], axis=0)
        # the z difference between these two points
        return point3D_head[:, 2] - point3D_feet[:, 2]

    def getUndistortMap(self, extent=None, scaling=None):
        # if no extent is given, take the maximum extent from the image border
        if extent is None:
            extent = [0, self.image_width_px, 0, self.image_height_px]

        # if we have cached the map, use the cached map
        if self.map_undistort is not None and \
                self.last_extent_undistort == extent and \
                self.last_scaling_undistort == scaling:
            return self.map_undistort

        # if no scaling is given, scale so that the resulting image has an equal amount of pixels as the original image
        if scaling is None:
            scaling = 1

        # get a mesh grid
        mesh = np.array(np.meshgrid(np.arange(extent[0], extent[1], scaling),
                                    np.arange(extent[2], extent[3], scaling)))

        # convert it to a list of points Nx2
        mesh_points = mesh.reshape(2, mesh.shape[1] * mesh.shape[2]).T

        # transform the space points to the image
        mesh_points_shape = self.lens.distortedFromImage(mesh_points)

        # reshape the map and cache it
        self.map_undistort = mesh_points_shape.T.reshape(mesh.shape).astype(np.float32)[:, ::-1, :]

        self.last_extent_undistort = extent
        self.last_scaling_undistort = scaling

        # return the calculated map
        return self.map_undistort

    def undistortImage(self, im, extent=None, scaling=None, do_plot=False, alpha=None):
        x, y = self.getUndistortMap(extent=extent, scaling=scaling)
        # ensure that the image has an alpha channel (to enable alpha for the points outside the image)
        if len(im.shape) == 2:
            pass
        elif im.shape[2] == 3:
            im = np.dstack((im, np.ones(shape=(im.shape[0], im.shape[1], 1), dtype="uint8") * 255))
        image = cv2.remap(im, x, y,
                          interpolation=cv2.INTER_NEAREST,
                          borderValue=[0, 1, 0, 0])[::-1]  # , borderMode=cv2.BORDER_TRANSPARENT)
        if do_plot:
            extent = self.last_extent_undistort.copy()
            extent[2], extent[3] = extent[3]-1, extent[2]-1
            plt.imshow(image, extent=extent, alpha=alpha)
        return image

    def getMap(self, extent=None, scaling=None, Z=0):
        # if no extent is given, take the maximum extent from the image border
        if extent is None:
            border = self.getImageBorder()
            extent = [np.nanmin(border[:, 0]), np.nanmax(border[:, 0]),
                      np.nanmin(border[:, 1]), np.nanmax(border[:, 1])]

        # if we have cached the map, use the cached map
        if self.map is not None and \
                self.last_extent == extent and \
                self.last_scaling == scaling:
            return self.map

        # if no scaling is given, scale so that the resulting image has an equal amount of pixels as the original image
        if scaling is None:
            scaling = np.sqrt((extent[1] - extent[0]) * (extent[3] - extent[2])) / \
                      np.sqrt((self.projection.parameters.image_width_px * self.projection.parameters.image_height_px))

        # get a mesh grid
        mesh = np.array(np.meshgrid(np.arange(extent[0], extent[1], scaling),
                                    np.arange(extent[2], extent[3], scaling)))

        # convert it to a list of points Nx2
        mesh_points = mesh.reshape(2, mesh.shape[1] * mesh.shape[2]).T
        mesh_points = np.hstack((mesh_points, Z*np.ones((mesh_points.shape[0], 1))))

        # transform the space points to the image
        mesh_points_shape = self.imageFromSpace(mesh_points)

        # reshape the map and cache it
        self.map = mesh_points_shape.T.reshape(mesh.shape).astype(np.float32)[:, ::-1, :]

        self.last_extent = extent
        self.last_scaling = scaling

        # return the calculated map
        return self.map

    def getTopViewOfImage(self, im, extent=None, scaling=None, do_plot=False, alpha=None, Z=0.):
        x, y = self.getMap(extent=extent, scaling=scaling, Z=Z)
        # ensure that the image has an alpha channel (to enable alpha for the points outside the image)
        if len(im.shape) == 2:
            pass
        elif im.shape[2] == 3:
            im = np.dstack((im, np.ones(shape=(im.shape[0], im.shape[1], 1), dtype="uint8") * 255))
        image = cv2.remap(im, x, y,
                          interpolation=cv2.INTER_NEAREST,
                          borderValue=[0, 1, 0, 0])  # , borderMode=cv2.BORDER_TRANSPARENT)
        if do_plot:
            plt.imshow(image, extent=self.last_extent, alpha=alpha)
        return image

    def generateLUT(self, undef_value=0):
        """
        Generate LUT to calculate area covered by one pixel in the image dependent on y position in the image

        Parameters
        ----------
        undef_value: number, optional
            what values undefined positions should have, default=0

        Returns
        -------
        LUT: ndarray
            same length as image height
        """

        def get_square(x, y):
            p0 = [x - 0.5, y - 0.5]
            p1 = [x + 0.5, y - 0.5]
            p2 = [x + 0.5, y + 0.5]
            p3 = [x - 0.5, y + 0.5]
            return np.array([p0, p1, p2, p3])

        def calc_quadrilateral_size(rect):
            A, B, C, D = rect
            return 0.5 * abs((A[1] - C[1]) * (D[0] - B[0]) + (B[1] - D[1]) * (A[0] - C[0]))

        x = self.image_width_px / 2

        horizon = self.getImageHorizon([x])
        y_stop = max([0, int(horizon[0, 1])])
        y_start = self.image_height_px

        y_lookup = np.zeros(self.image_height_px) + undef_value

        for y in range(y_stop, y_start):
            rect = get_square(x, y)
            rect = self.spaceFromImage(rect, Z=0)
            A = calc_quadrilateral_size(rect)
            y_lookup[y] = A

        return y_lookup

    def save(self, filename):
        """
        Saves the camera parameters to a json file.

        Parameters
        ----------
        filename : str
            the filename where to store the parameters.
        """
        keys = self.parameters.parameters.keys()
        export_dict = {key: getattr(self, key) for key in keys}
        with open(filename, "w") as fp:
            fp.write(json.dumps(export_dict, indent=4))

    def load(self, filename):
        """
        Load the camera parameters from a json file.

        Parameters
        ----------
        filename : str
            the filename of the file to load.
        """
        with open(filename, "r") as fp:
            variables = json.loads(fp.read())
        for key in variables:
            setattr(self, key, variables[key])


def load_camera(filename):
    """
    Create a :py:class:`Camera` instance with the parameters from the file.

    Parameters
    ----------
    filename : str
        the filename of the file to load.

    Returns
    -------
    camera : :py:class:`Camera`
        the camera with the given parameters.
    """
    cam = Camera(RectilinearProjection(), SpatialOrientation())
    cam.load(filename)
    return cam
