#include <sensor_msgs/msg/imu.hpp>
#include "rp_exp/timed_subscriber.hpp"

int main(int argc, char ** argv) {
  rclcpp::init(argc, argv);
  const auto args = rclcpp::remove_ros_arguments(argc, argv);
  const std::string topic = args.size() > 1 ? args[1] : "/imu";
#ifdef RELIABLE_QOS
  constexpr bool reliable = true;
#else
  constexpr bool reliable = false;
#endif
  auto node = std::make_shared<rp_exp::TimedSubscriber<sensor_msgs::msg::Imu>>(
    "s2_subscriber", topic, 200,
    rp_exp::seconds_from_env("RP_EXP_WARMUP_SEC", 0),
    rp_exp::seconds_from_env("RP_EXP_MEASURE_SEC", 60), reliable);
  rclcpp::spin(node);
  rclcpp::shutdown();
  return 0;
}
