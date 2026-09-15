#include <sensor_msgs/msg/image.hpp>
#include "rp_exp/timed_subscriber.hpp"

int main(int argc, char ** argv) {
  rclcpp::init(argc, argv);
  const auto args = rclcpp::remove_ros_arguments(argc, argv);
  const double hz = args.size() > 1 ? std::stod(args[1]) : 1000;
  const int warmup = args.size() > 2 ? std::stoi(args[2]) : 10;
  const int duration = args.size() > 3 ? std::stoi(args[3]) : 60;
  const std::string topic = args.size() > 4 ? args[4] : "/stress";
  auto node = std::make_shared<rp_exp::TimedSubscriber<sensor_msgs::msg::Image>>(
    "stress_subscriber", topic, hz, warmup, duration, false);
  rclcpp::spin(node);
  rclcpp::shutdown();
  return 0;
}
