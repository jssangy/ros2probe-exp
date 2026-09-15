# N_P개 discovery_test 노드를 같은 인자로 동시에 띄우는 launch (slave_controller에서 사용)

import launch
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def create_nodes(context, *args, **kwargs):
    n_p = int(LaunchConfiguration("n_p").perform(context))
    n_e = LaunchConfiguration("n_e").perform(context)
    if n_p not in (1, 3, 5) or int(n_e) != 2:
        raise ValueError('Paper discovery requires NP=1, 3, 5 and exactly two endpoints per participant')
    target_time = LaunchConfiguration("target_time").perform(context)
    run_duration = LaunchConfiguration("run_duration").perform(context)
    single_topic = LaunchConfiguration("single_topic").perform(context)
    topic_report_dir = LaunchConfiguration("topic_report_dir").perform(context)
    total_processes = LaunchConfiguration("total_processes").perform(context) or str(n_p)

    additional_env = {"DISCOVERY_TOPIC_REPORT_DIR": topic_report_dir} if topic_report_dir else {}

    nodes = []
    for i in range(n_p):
        nodes.append(
            Node(
                package="rclcpp_discovery_traffic",
                executable="discovery_test",
                name="discovery_stress_node_" + str(i),
                arguments=[n_e, target_time, run_duration, single_topic, str(i), total_processes],
                output="log",
                additional_env=additional_env,
            )
        )
    return nodes


def generate_launch_description():
    return launch.LaunchDescription([
        DeclareLaunchArgument("n_p", default_value="1", description="슬레이브당 discovery_test 개수"),
        DeclareLaunchArgument("n_e", default_value="2", description="One publisher and one subscriber"),
        DeclareLaunchArgument("target_time", description="목표 시작 시각 (Unix timestamp)"),
        DeclareLaunchArgument("run_duration", default_value="7", description="실행 유지 시간(초)"),
        DeclareLaunchArgument("single_topic", default_value="0", description="0=분배, 1=한 토픽, 2=랜덤(0..N_E)"),
        DeclareLaunchArgument("total_processes", default_value="", description="실험 전체 discovery_test 개수 (비면 n_p 사용)"),
        DeclareLaunchArgument("topic_report_dir", default_value="", description="토픽 리포트 디렉터리 (비면 미기록)"),
        OpaqueFunction(function=create_nodes),
    ])
