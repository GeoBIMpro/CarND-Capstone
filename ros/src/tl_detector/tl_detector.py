#!/usr/bin/env python
import rospy
from std_msgs.msg import Int32
from geometry_msgs.msg import PointStamped, PoseStamped, Pose
from styx_msgs.msg import TrafficLightArray, TrafficLight, Waypoint, Lane
from sensor_msgs.msg import Image, CameraInfo
from cv_bridge import CvBridge
from light_classification.tl_classifier import TLClassifier
from image_geometry import PinholeCameraModel
import tf
import cv2
import yaml
import math

from waypoint_updater.srv import *

STATE_COUNT_THRESHOLD = 3

class TLDetector(object):
    def __init__(self):
        rospy.init_node('tl_detector')

        self.pose = None
        self.waypoints = None
        self.camera_image = None
        self.state = None
        self.lights = []

        rospy.Subscriber('/current_pose', PoseStamped, self.pose_cb)
        rospy.Subscriber('/base_waypoints', Lane, self.waypoints_cb)

        '''
        /vehicle/traffic_lights provides you with the location of the traffic light in 3D map space and
        helps you acquire an accurate ground truth data source for the traffic light
        classifier by sending the current color state of all traffic lights in the
        simulator. When testing on the vehicle, the color state will not be available. You'll need to
        rely on the position of the light and the camera image to predict it.
        '''
        rospy.Subscriber('/vehicle/traffic_lights', TrafficLightArray, self.traffic_cb)
        rospy.Subscriber('/image_color', Image, self.image_cb)
        rospy.Subscriber('/camera_info', CameraInfo, self.camera_info_cb)

        config_string = rospy.get_param("/traffic_light_config")
        self.config = yaml.load(config_string)
        self.max_visible_distance = rospy.get_param('~max_visible_distance')

        self.upcoming_red_light_pub = rospy.Publisher('/traffic_waypoint', Int32, queue_size=1)
        self.next_waypoint_proxy = rospy.ServiceProxy('/waypoint_updater/next_waypoint', NextWaypoint)

        self.bridge = CvBridge()
        self.light_classifier = TLClassifier()
        self.listener = tf.TransformListener()

        self.state = TrafficLight.UNKNOWN
        self.last_state = TrafficLight.UNKNOWN
        self.last_wp = -1
        self.state_count = 0

        rospy.spin()

    def pose_cb(self, msg):
        self.pose = msg

    def waypoints_cb(self, waypoints):
        self.waypoints = waypoints

    def traffic_cb(self, msg):
        self.lights = msg.lights

    def camera_info_cb(self, msg):
        self.camera_info = msg

    def image_cb(self, msg):
        """Identifies red lights in the incoming camera image and publishes the index
            of the waypoint closest to the red light's stop line to /traffic_waypoint

        Args:
            msg (Image): image from car-mounted camera

        """
        self.has_image = True
        self.camera_image = msg
        light_wp, state = self.process_traffic_lights()

        '''
        Publish upcoming red lights at camera frequency.
        Each predicted state has to occur `STATE_COUNT_THRESHOLD` number
        of times till we start using it. Otherwise the previous stable state is
        used.
        '''
        if self.state != state:
            self.state_count = 0
            self.state = state
        elif self.state_count >= STATE_COUNT_THRESHOLD:
            self.last_state = self.state
            light_wp = light_wp if state == TrafficLight.RED else -1
            self.last_wp = light_wp
            self.upcoming_red_light_pub.publish(Int32(light_wp))
        else:
            self.upcoming_red_light_pub.publish(Int32(self.last_wp))
        self.state_count += 1

    def get_closest_waypoint(self, pose, waypoints):
        """Identifies the closest path waypoint to the given position
            https://en.wikipedia.org/wiki/Closest_pair_of_points_problem
        Args:
            pose (Pose): position to match a waypoint to
            waypoints (Waypoints[]): waypoints to check against

        Returns:
            int: index of the closest waypoint in self.waypoints

        """
        msg = NextWaypointRequest(pose=pose, waypoints=waypoints)
        response = self.next_waypoint_proxy(msg)
        return response.next_waypoint.data

    def get_light_state(self, light):
        """Determines the current color of the traffic light

        Args:
            light (TrafficLight): light to classify

        Returns:
            int: ID of traffic light color (specified in styx_msgs/TrafficLight)

        """

        """
        TODO: !!! remove this return statement !!!
        this only works on the simulator because we are given ground truth data for light state
        replace when classifier is complete
        """
        return light.state

        if(not self.has_image):
            self.prev_light_loc = None
            return False

        cv_image = self.bridge.imgmsg_to_cv2(self.camera_image, "bgr8")

        #Get classification
        return self.light_classifier.get_classification(cv_image)

    def process_traffic_lights(self):
        """Finds closest visible traffic light, if one exists, and determines its
            location and color

        Returns:
            int: index of waypoint closest to the upcoming stop line for a traffic light (-1 if none exists)
            int: ID of traffic light color (specified in styx_msgs/TrafficLight)

        """
        light = None

        # List of positions that correspond to the line to stop in front of for a given intersection
        stop_line_positions = self.config['stop_line_positions']

        if (self.pose and self.waypoints and self.lights):
            car_position = self.get_closest_waypoint(self.pose, self.waypoints.waypoints)
            waypoint = self.waypoints.waypoints[car_position]

            light_waypoints = [Waypoint(pose=x.pose) for x in self.lights]
            light = self.get_closest_waypoint(waypoint.pose, light_waypoints)


        if (self.pose and self.waypoints and self.lights and self.camera_info):

            light_waypoints = [Waypoint(pose=x.pose) for x in self.lights]
            closest_idx = self.get_closest_waypoint(self.pose, light_waypoints)
            closest_light = self.lights[closest_idx]

            # if light is visible, we want to return the waypoint
            if self.is_visible(self.pose, closest_light.pose.pose.position):
                light = closest_light

                # get waypoint closest to upcoming light
                closet_waypoint = self.get_closest_waypoint(closest_light.pose, self.waypoints.waypoints)
                light_wp = closet_waypoint - 1 # -1 so it's just before the light
                if light_wp < 0:
                    light_wp = len(closet_waypoint) - light_wp

        if light:
            state = self.get_light_state(light)
            return light_wp, state

        return -1, TrafficLight.UNKNOWN

    def transform_point(self, pose, position):
        """
        create point so we can transform perspective to use camera as reference frame
        todo: for precise measurement, transform in reference to camera instead of /base_link

        Args:
            pose (Pose): current pose of vehicle
            position (Position): position to check

        Returns:
            point (PointStamped): transformed point
        """
        header = pose.header
        p_world = PointStamped()
        p_world.header.seq = header.seq
        p_world.header.stamp = header.stamp
        p_world.header.frame_id = '/world'
        p_world.point.x = position.x
        p_world.point.y = position.y
        p_world.point.z = position.z

        self.listener.waitForTransform('/base_link', '/world', header.stamp, rospy.Duration(3.0))
        return self.listener.transformPoint('/base_link', p_world)

    def is_visible(self, pose, position):
        """
        check if point is visible by projecting it onto 2d camera plane
        todo: distance check to prevent very far away points from passing

        Args:
            pose (Pose): current pose of vehicle
            position (Position): position to check

        Returns:
            bool: if point is visible
        """

        transformed = self.transform_point(pose, position)
        point = transformed.point

        # pinhole camera model reverses x, y coordinates
        x = -point.y # point.y = horizontal camera dimension
        y = -point.z # point.z = vertical camera dimension
        z = point.x # point.x = z/depth camera dimension

        # todo: does this need to be more elegant?
        if z > self.max_visible_distance:
            return False

        # create camera info
        camera = PinholeCameraModel()
        camera.fromCameraInfo(self.camera_info)

        # project point onto image
        u, v = camera.project3dToPixel((x, y, z))

        # setup bounding box
        cx = camera.cx()
        cy = camera.cy()
        width = self.camera_info.width
        height = self.camera_info.height
        top = cy + height / 2
        bottom = cy - height / 2
        right = cx + width / 2
        left = cx - width / 2

        # light is visible if projected within the bounding box of the 2d image
        return left <= u and u <= right and bottom <= v and v <= top

if __name__ == '__main__':
    try:
        TLDetector()
    except rospy.ROSInterruptException:
        rospy.logerr('Could not start traffic node.')
