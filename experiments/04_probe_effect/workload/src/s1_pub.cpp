#include <chrono>
#include <memory>
#include <stdexcept>

#include <rclcpp/rclcpp.hpp>
#include <geometry_msgs/msg/twist_stamped.hpp>

#ifdef RELIABLE_QOS
static constexpr const char * QOS_LABEL = "RELIABLE";
#else
static constexpr const char * QOS_LABEL = "BEST_EFFORT";
#endif

class S1Publisher : public rclcpp::Node
{
public:
  explicit S1Publisher(double hz, uint64_t max_count)
  : Node("s1_publisher"), pub_count_(0), max_count_(max_count)
  {
    rclcpp::QoS qos(1);
#ifdef RELIABLE_QOS
    qos.reliable();
#else
    qos.best_effort();
#endif
    pub_ = create_publisher<geometry_msgs::msg::TwistStamped>("/cmd_vel", qos);

    msg_ = std::make_shared<geometry_msgs::msg::TwistStamped>();
    msg_->header.frame_id    = "base_link";
    msg_->twist.linear.x     = 0.5;
    msg_->twist.linear.y     = 0.0;
    msg_->twist.linear.z     = 0.0;
    msg_->twist.angular.x    = 0.0;
    msg_->twist.angular.y    = 0.0;
    msg_->twist.angular.z    = 0.3;

    auto period = std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::duration<double>(1.0 / hz));
    timer_ = create_wall_timer(period, [this]() { timer_cb(); });

    RCLCPP_INFO(get_logger(), "S1 publisher started: %.1f Hz, max %lu msgs, %s",
      hz, max_count_, QOS_LABEL);
  }

private:
  void timer_cb()
  {
    msg_->header.stamp = now();
    pub_->publish(*msg_);
    ++pub_count_;

    if (max_count_ > 0 && pub_count_ >= max_count_) {
      RCLCPP_INFO(get_logger(), "DONE: published %lu msgs", pub_count_);
      timer_->cancel();
      rclcpp::shutdown();
      return;
    }
    if (pub_count_ % 200 == 0) {
      RCLCPP_INFO(get_logger(), "published %lu msgs", pub_count_);
    }
  }

  rclcpp::Publisher<geometry_msgs::msg::TwistStamped>::SharedPtr pub_;
  rclcpp::TimerBase::SharedPtr timer_;
  geometry_msgs::msg::TwistStamped::SharedPtr msg_;
  uint64_t pub_count_;
  uint64_t max_count_;
};

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);
  auto args = rclcpp::remove_ros_arguments(argc, argv);

  double hz = 20.0;
  if (args.size() > 1) {
    hz = std::stod(args[1]);
  }
  if (hz > 50.0) {
    RCLCPP_WARN(rclcpp::get_logger("s1_pub"), "Hz %.1f > 50 (S1 상한), 50으로 제한", hz);
    hz = 50.0;
  }
  uint64_t max_count = 0;  // 0 = infinite; subscriber controls measurement window
  if (args.size() > 2) {
    max_count = std::stoull(args[2]);
  }

  auto node = std::make_shared<S1Publisher>(hz, max_count);

  rclcpp::executors::MultiThreadedExecutor exec;
  exec.add_node(node);
  exec.spin();

  rclcpp::shutdown();
  return 0;
}
