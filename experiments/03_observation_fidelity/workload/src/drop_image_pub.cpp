#include <chrono>
#include <fstream>
#include <atomic>
#include <cstring>
#include <cstdint>
#include <memory>
#include <stdexcept>
#include <string>
#include <thread>

#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/image.hpp>

class DropImagePublisher : public rclcpp::Node
{
public:
  DropImagePublisher(double hz, std::size_t payload_bytes, uint64_t expected_count,
                     const std::string & topic, double match_timeout_sec, double settle_sec,
                     int required_readers, const std::string & gate)
  : Node("drop_image_publisher"),
    hz_(hz),
    expected_count_(expected_count),
    match_timeout_sec_(match_timeout_sec),
    settle_sec_(settle_sec), required_readers_(required_readers), gate_(gate)
  {
    rclcpp::QoS qos(1);
    qos.best_effort();
    pub_ = create_publisher<sensor_msgs::msg::Image>(topic, qos);

    msg_ = std::make_shared<sensor_msgs::msg::Image>();
    msg_->header.frame_id = "drop_image_frame";
    msg_->height = 1;
    msg_->width = static_cast<uint32_t>(payload_bytes);
    msg_->encoding = "mono8";
    msg_->is_bigendian = 0;
    msg_->step = static_cast<uint32_t>(payload_bytes);
    msg_->data.resize(payload_bytes, 0);

    publish_thread_ = std::thread(&DropImagePublisher::publish_loop, this);

    RCLCPP_INFO(
      get_logger(),
      "Drop image publisher ready: topic=%s hz=%.1f payload_bytes=%zu expected=%lu qos=BEST_EFFORT",
      topic.c_str(), hz_, payload_bytes, expected_count_);
  }

  ~DropImagePublisher() override
  {
    if (publish_thread_.joinable()) {
      publish_thread_.join();
    }
  }

private:
  void publish_loop()
  {
    if (!wait_for_match()) { rclcpp::shutdown(); return; }
    if (settle_sec_ > 0.0) {
      std::this_thread::sleep_for(std::chrono::duration<double>(settle_sec_));
    }

    std::printf("PUBLISH_READY readers=%d\n", required_readers_);
    std::fflush(stdout);
    const auto gate_deadline = std::chrono::steady_clock::now() + std::chrono::seconds(45);
    while (!gate_.empty() && !std::ifstream(gate_).good()) {
      if (!rclcpp::ok() || std::chrono::steady_clock::now() >= gate_deadline) {
        std::fprintf(stderr, "[ERROR] publication gate was not released\n");
        rclcpp::shutdown();
        return;
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    using clock = std::chrono::steady_clock;
    const auto period = std::chrono::duration_cast<clock::duration>(
      std::chrono::duration<double>(1.0 / hz_));
    auto next = clock::now();

    std::printf("PUBLISH_START expected=%lu\n", expected_count_);
    std::fflush(stdout);

    uint64_t published = 0;
    for (uint64_t seq = 1; rclcpp::ok() && seq <= expected_count_; ++seq) {
      std::memcpy(msg_->data.data(), &seq, sizeof(seq));
      msg_->header.stamp = now();
      pub_->publish(*msg_);
      published = seq;
      if (seq % 300 == 0) {
        RCLCPP_INFO(get_logger(), "published seq=%lu", seq);
      }
      next += period;
      std::this_thread::sleep_until(next);
      const auto now_time = clock::now();
      if (next < now_time) {
        next = now_time;
      }
    }

    if (published == expected_count_) std::printf("PUBLISH_DONE expected=%lu\n", published);
    else std::printf("PUBLISH_ABORTED sent=%lu\n", published);
    std::fflush(stdout);
    rclcpp::shutdown();
  }

  bool wait_for_match()
  {
    using clock = std::chrono::steady_clock;
    const auto deadline = clock::now() + std::chrono::duration<double>(match_timeout_sec_);
    while (rclcpp::ok() && clock::now() < deadline) {
      if (pub_->get_subscription_count() >= static_cast<size_t>(required_readers_)) {
        RCLCPP_INFO(get_logger(), "subscriber matched: count=%zu", pub_->get_subscription_count());
        return true;
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    RCLCPP_ERROR(get_logger(), "required readers did not match before timeout");
    return false;
  }

  rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr pub_;
  sensor_msgs::msg::Image::SharedPtr msg_;
  double hz_;
  uint64_t expected_count_;
  double match_timeout_sec_;
  double settle_sec_;
  int required_readers_;
  std::string gate_;
  std::thread publish_thread_;
};

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);
  auto args = rclcpp::remove_ros_arguments(argc, argv);

  double hz = 30.0;
  if (args.size() > 1) {
    hz = std::stod(args[1]);
  }
  std::size_t payload_bytes = 1024;
  if (args.size() > 2) {
    payload_bytes = static_cast<std::size_t>(std::stoull(args[2]));
  }
  uint64_t expected_count = 1800;
  if (args.size() > 3) {
    expected_count = std::stoull(args[3]);
  }
  std::string topic = "/drop_image";
  if (args.size() > 4) {
    topic = args[4];
  }
  double match_timeout_sec = 10.0;
  if (args.size() > 5) {
    match_timeout_sec = std::stod(args[5]);
  }
  double settle_sec = 3.0;
  if (args.size() > 6) {
    settle_sec = std::stod(args[6]);
  }

  const int required_readers = args.size() > 7 ? std::stoi(args[7]) : 1;
  const std::string gate = args.size() > 8 ? args[8] : "";

  if (required_readers < 1 || hz <= 0.0 || payload_bytes < sizeof(uint64_t) || expected_count == 0) {
    throw std::invalid_argument("invalid hz, payload_bytes, or expected_count");
  }

  auto node = std::make_shared<DropImagePublisher>(
    hz, payload_bytes, expected_count, topic, match_timeout_sec, settle_sec, required_readers, gate);
  rclcpp::executors::MultiThreadedExecutor exec;
  exec.add_node(node);
  exec.spin();
  rclcpp::shutdown();
  return 0;
}
