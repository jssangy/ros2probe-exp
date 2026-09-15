#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <cstdint>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/image.hpp>

class DropImageSubscriber : public rclcpp::Node
{
public:
  DropImageSubscriber(double hz, uint64_t expected_count, std::size_t payload_bytes,
                      const std::string & topic, double timeout_sec)
  : Node("drop_image_subscriber"),
    hz_(hz),
    expected_count_(expected_count),
    payload_bytes_(payload_bytes),
    timeout_sec_(timeout_sec),
    start_time_(std::chrono::steady_clock::now()),
    first_seq_seen_(false),
    finalized_(false),
    seen_(expected_count + 1, false)
  {
    rclcpp::QoS qos(1);
    qos.best_effort();
    sub_ = create_subscription<sensor_msgs::msg::Image>(
      topic, qos,
      [this](sensor_msgs::msg::Image::ConstSharedPtr msg) {
        handle_msg(*msg);
      });

    watchdog_thread_ = std::thread(&DropImageSubscriber::watchdog_loop, this);

    std::printf(
      "CONFIG topic=%s hz=%.1f payload_bytes=%zu expected=%lu qos=best_effort\n",
      topic.c_str(), hz_, payload_bytes_, expected_count_);
    std::printf("SUB_READY\n");
    std::fflush(stdout);
  }

  ~DropImageSubscriber() override
  {
    finalized_ = true;
    if (watchdog_thread_.joinable()) {
      watchdog_thread_.join();
    }
  }

private:
  void handle_msg(const sensor_msgs::msg::Image & msg)
  {
    if (msg.data.size() < sizeof(uint64_t)) {
      return;
    }
    uint64_t seq = 0;
    std::memcpy(&seq, msg.data.data(), sizeof(seq));
    if (seq == 0 || seq > expected_count_) {
      return;
    }

    bool is_new = false;
    {
      std::lock_guard<std::mutex> lk(mtx_);
      if (!first_seq_seen_) {
        first_seq_seen_ = true;
        first_seq_time_ = std::chrono::steady_clock::now();
      }
      if (!seen_[seq]) {
        seen_[seq] = true;
        is_new = true;
        ++recv_unique_;
      }
    }

    if (is_new) {
      std::printf("SEQ %lu\n", seq);
      std::fflush(stdout);
    }

    if (recv_unique_.load() >= expected_count_) {
      finalize();
    }
  }

  void watchdog_loop()
  {
    using namespace std::chrono_literals;
    while (rclcpp::ok() && !finalized_.load()) {
      std::this_thread::sleep_for(100ms);
      bool expired = false;
      {
        std::lock_guard<std::mutex> lk(mtx_);
        const auto now = std::chrono::steady_clock::now();
        const auto reference = first_seq_seen_ ? first_seq_time_ : start_time_;
        expired = std::chrono::duration<double>(now - reference).count() >= timeout_sec_;
      }
      if (expired) {
        finalize();
        return;
      }
    }
  }

  void finalize()
  {
    bool expected = false;
    if (!finalized_.compare_exchange_strong(expected, true)) {
      return;
    }
    const uint64_t recv = recv_unique_.load();
    const uint64_t capped = std::min(recv, expected_count_);
    const double drop = 100.0 * static_cast<double>(expected_count_ - capped) /
      static_cast<double>(expected_count_);
    std::printf("FINAL [60s]: recv %lu / expected %lu -> drop %.1f%%\n",
      recv, expected_count_, drop);
    std::fflush(stdout);
    rclcpp::shutdown();
  }

  rclcpp::Subscription<sensor_msgs::msg::Image>::SharedPtr sub_;
  double hz_;
  uint64_t expected_count_;
  std::size_t payload_bytes_;
  double timeout_sec_;
  std::chrono::steady_clock::time_point start_time_;
  std::atomic<bool> first_seq_seen_;
  std::atomic<bool> finalized_;
  std::atomic<uint64_t> recv_unique_{0};
  std::vector<bool> seen_;
  std::mutex mtx_;
  std::chrono::steady_clock::time_point first_seq_time_;
  std::thread watchdog_thread_;
};

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);
  auto args = rclcpp::remove_ros_arguments(argc, argv);

  double hz = 30.0;
  if (args.size() > 1) {
    hz = std::stod(args[1]);
  }
  uint64_t expected_count = 1800;
  if (args.size() > 2) {
    expected_count = std::stoull(args[2]);
  }
  std::size_t payload_bytes = 1024;
  if (args.size() > 3) {
    payload_bytes = static_cast<std::size_t>(std::stoull(args[3]));
  }
  std::string topic = "/drop_image";
  if (args.size() > 4) {
    topic = args[4];
  }
  double timeout_sec = 62.0;
  if (args.size() > 5) {
    timeout_sec = std::stod(args[5]);
  }

  if (hz <= 0.0 || expected_count == 0 || payload_bytes < sizeof(uint64_t) || timeout_sec <= 0.0) {
    throw std::invalid_argument("invalid hz, expected_count, payload_bytes, or timeout_sec");
  }

  auto node = std::make_shared<DropImageSubscriber>(
    hz, expected_count, payload_bytes, topic, timeout_sec);
  rclcpp::executors::MultiThreadedExecutor exec;
  exec.add_node(node);
  exec.spin();
  rclcpp::shutdown();
  return 0;
}
