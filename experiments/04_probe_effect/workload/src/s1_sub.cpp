#include <geometry_msgs/msg/twist_stamped.hpp>
#include "rp_exp/timed_subscriber.hpp"

int main(int argc, char ** argv) {
  rclcpp::init(argc, argv);
  const auto args = rclcpp::remove_ros_arguments(argc, argv);
  const std::string topic = args.size() > 1 ? args[1] : "/cmd_vel";
#ifdef RELIABLE_QOS
  constexpr bool reliable = true;
#else
  constexpr bool reliable = false;
#endif
  auto node = std::make_shared<rp_exp::TimedSubscriber<geometry_msgs::msg::TwistStamped>>(
    "s1_subscriber", topic, 20,
    rp_exp::seconds_from_env("RP_EXP_WARMUP_SEC", 0),
    rp_exp::seconds_from_env("RP_EXP_MEASURE_SEC", 60), reliable);
  rclcpp::spin(node);
  rclcpp::shutdown();
  return 0;
}
