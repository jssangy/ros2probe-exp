from launch import LaunchDescription
from launch_ros.actions import Node


# S5-b reliable pub: high-bandwidth autonomous driving
def generate_launch_description():
    return LaunchDescription([
        Node(package='rp_exp_probe_effect', executable='s1_pub_rel', name='cmd_vel_publisher'),
        Node(package='rp_exp_probe_effect', executable='s2_pub_rel', name='imu_publisher'),
        Node(package='rp_exp_probe_effect', executable='s3_points_pub_rel',
             name='points_front_publisher',
             arguments=['130000'],
             remappings=[('/points', '/points/front')]),
        Node(package='rp_exp_probe_effect', executable='s3_points_pub_rel',
             name='points_rear_publisher',
             arguments=['30000'],
             remappings=[('/points', '/points/rear')]),
        Node(package='rp_exp_probe_effect', executable='s4a_pub_rel',
             name='camera_front_publisher',
             remappings=[('/image_raw/compressed', '/camera/front/compressed')]),
        Node(package='rp_exp_probe_effect', executable='s4a_pub_rel',
             name='camera_left_publisher',
             remappings=[('/image_raw/compressed', '/camera/left/compressed')]),
        Node(package='rp_exp_probe_effect', executable='s4a_pub_rel',
             name='camera_right_publisher',
             remappings=[('/image_raw/compressed', '/camera/right/compressed')]),
        Node(package='rp_exp_probe_effect', executable='s4a_pub_rel',
             name='camera_rear_publisher',
             remappings=[('/image_raw/compressed', '/camera/rear/compressed')]),
        Node(package='rp_exp_probe_effect', executable='s4_image_pub_rel',
             name='depth_publisher',
             arguments=['640', '480', '16UC1']),
    ])
