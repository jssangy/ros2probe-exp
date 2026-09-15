from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


# S5-b reliable sub: high-bandwidth autonomous driving
def generate_launch_description():
    include_points_front = LaunchConfiguration('include_points_front')

    return LaunchDescription([
        DeclareLaunchArgument('include_points_front', default_value='true'),
        Node(package='rp_exp_probe_effect', executable='s1_sub_rel',
             name='cmd_vel_subscriber', output='screen', emulate_tty=True),
        Node(package='rp_exp_probe_effect', executable='s2_sub_rel',
             name='imu_subscriber', output='screen', emulate_tty=True),
        Node(package='rp_exp_probe_effect', executable='s3_points_sub_rel',
             name='points_front_subscriber', output='screen', emulate_tty=True,
             condition=IfCondition(include_points_front),
             remappings=[('/points', '/points/front')]),
        Node(package='rp_exp_probe_effect', executable='s3_points_sub_rel',
             name='points_rear_subscriber', output='screen', emulate_tty=True,
             remappings=[('/points', '/points/rear')]),
        Node(package='rp_exp_probe_effect', executable='s4a_sub_rel',
             name='camera_front_subscriber',
             output='screen', emulate_tty=True,
             remappings=[('/image_raw/compressed', '/camera/front/compressed')]),
        Node(package='rp_exp_probe_effect', executable='s4a_sub_rel',
             name='camera_left_subscriber',
             output='screen', emulate_tty=True,
             remappings=[('/image_raw/compressed', '/camera/left/compressed')]),
        Node(package='rp_exp_probe_effect', executable='s4a_sub_rel',
             name='camera_right_subscriber',
             output='screen', emulate_tty=True,
             remappings=[('/image_raw/compressed', '/camera/right/compressed')]),
        Node(package='rp_exp_probe_effect', executable='s4a_sub_rel',
             name='camera_rear_subscriber',
             output='screen', emulate_tty=True,
             remappings=[('/image_raw/compressed', '/camera/rear/compressed')]),
        Node(package='rp_exp_probe_effect', executable='s4_image_sub_rel',
             name='depth_subscriber',
             output='screen', emulate_tty=True,
             arguments=['/depth/image_raw']),
    ])
