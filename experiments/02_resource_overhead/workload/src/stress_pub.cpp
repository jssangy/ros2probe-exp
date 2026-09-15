#include <atomic>
#include <chrono>
#include <cstdint>
#include <memory>
#include <stdexcept>
#include <string>
#include <thread>

#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/image.hpp>

class StressPublisher : public rclcpp::Node
{
public:
  StressPublisher(double hz, std::size_t payload_bytes, uint64_t max_count, const std::string & topic)
  : Node("stress_publisher"),
    hz_(hz),
    max_count_(max_count),
    running_(true),
    pub_count_(0)
  {
    rclcpp::QoS qos(1);
    qos.best_effort();
    pub_ = create_publisher<sensor_msgs::msg::Image>(topic, qos);

    msg_ = std::make_shared<sensor_msgs::msg::Image>();
    msg_->header.frame_id = "stress_frame";
    msg_->height = 1;
    msg_->width = static_cast<uint32_t>(payload_bytes);
    msg_->encoding = "mono8";
    msg_->is_bigendian = 0;
    msg_->step = static_cast<uint32_t>(payload_bytes);
    msg_->data.resize(payload_bytes);
    for (std::size_t i = 0; i < msg_->data.size(); ++i) {
      msg_->data[i] = static_cast<uint8_t>(i & 0xff);
    }

    pub_thread_ = std::thread(&StressPublisher::publish_loop, this);

    RCLCPP_INFO(
      get_logger(),
      "Stress publisher started: topic=%s, %.1f Hz, payload=%zu bytes, max=%lu, BEST_EFFORT",
      topic.c_str(), hz_, payload_bytes, max_count_);
  }

  ~StressPublisher() override
  {
    running_ = false;
    if (pub_thread_.joinable()) {
      pub_thread_.join();
    }
  }

private:
  void publish_loop()
  {
    using clock = std::chrono::steady_clock;
    const auto period = std::chrono::duration_cast<clock::duration>(
      std::chrono::duration<double>(1.0 / hz_));
    auto next = clock::now() + period;

    while (rclcpp::ok() && running_) {
      msg_->header.stamp = now();
      pub_->publish(*msg_);

      const auto count = ++pub_count_;
      if (max_count_ > 0 && count >= max_count_) {
        RCLCPP_INFO(get_logger(), "DONE: published %lu msgs", count);
        running_ = false;
        rclcpp::shutdown();
        break;
      }
      if (count % 10000 == 0) {
        RCLCPP_INFO(get_logger(), "published %lu msgs", count);
      }

      std::this_thread::sleep_until(next);
      next += period;
      const auto now_time = clock::now();
      if (next < now_time) {
        next = now_time + period;
      }
    }
  }

  rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr pub_;
  sensor_msgs::msg::Image::SharedPtr msg_;
  double hz_;
  uint64_t max_count_;
  std::atomic<bool> running_;
  std::atomic<uint64_t> pub_count_;
  std::thread pub_thread_;
};

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);
  auto args = rclcpp::remove_ros_arguments(argc, argv);

  double hz = 1000.0;
  if (args.size() > 1) {
    hz = std::stod(args[1]);
  }
  std::size_t payload_bytes = 65536;
  if (args.size() > 2) {
    payload_bytes = static_cast<std::size_t>(std::stoull(args[2]));
  }
  uint64_t max_count = 0;
  if (args.size() > 3) {
    max_count = std::stoull(args[3]);
  }
  std::string topic = "/stress";
  if (args.size() > 4) {
    topic = args[4];
  }

  if (hz <= 0.0) {
    throw std::invalid_argument("hz must be positive");
  }
  if (payload_bytes == 0) {
    throw std::invalid_argument("payload_bytes must be positive");
  }

  auto node = std::make_shared<StressPublisher>(hz, payload_bytes, max_count, topic);
  rclcpp::executors::MultiThreadedExecutor exec;
  exec.add_node(node);
  exec.spin();

  rclcpp::shutdown();
  return 0;
}
