from launch import LaunchDescription
from launch_ros.actions import Node


# S5-a pub: 실내 AMR
# /cmd_vel(20Hz) + /imu(200Hz) + /scan(40Hz) + /image_raw/compressed(30Hz)
def generate_launch_description():
    return LaunchDescription([
        Node(package='rp_exp_probe_effect', executable='s1_pub'),
        Node(package='rp_exp_probe_effect', executable='s2_pub'),
        Node(package='rp_exp_probe_effect', executable='s3a_pub'),
        Node(package='rp_exp_probe_effect', executable='s4a_pub'),
    ])
