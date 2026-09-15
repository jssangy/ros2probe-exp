#pragma once

#include <algorithm>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <rclcpp/rclcpp.hpp>

namespace rp_exp {
inline int seconds_from_env(const char * name, int fallback) {
  const char * value = std::getenv(name);
  return value ? std::stoi(value) : fallback;
}

// Count callback arrivals in [first arrival + warmup, start + duration).
// The deadline is independent of further arrivals: a stalled stream still ends.
template<class Message>
class TimedSubscriber : public rclcpp::Node {
  using Clock = std::chrono::steady_clock;
public:
  TimedSubscriber(const std::string & name, const std::string & topic, double hz,
                  int warmup, int duration, bool reliable)
  : Node(name), hz_(hz), warmup_(warmup), duration_(duration) {
    if (hz <= 0 || warmup < 0 || duration <= 0) {
      throw std::invalid_argument("invalid rate or measurement interval");
    }
    rclcpp::QoS qos(1);
    if (reliable) qos.reliable(); else qos.best_effort();
    sub_ = this->template create_subscription<Message>(topic, qos,
      [this](typename Message::ConstSharedPtr) { receive(); });
    topic_ = sub_->get_topic_name();
    std::printf("CONFIG topic=%s hz=%.3f warmup_sec=%d measure_sec=%d qos=%s\nSUB_READY\n",
      topic_.c_str(), hz_, warmup_, duration_, reliable ? "reliable" : "best_effort");
    std::fflush(stdout);
    worker_ = std::thread([this] { finish_at_deadline(); });
  }
  ~TimedSubscriber() override {
    { std::lock_guard<std::mutex> lock(mutex_); stopping_ = true; }
    cv_.notify_all();
    if (worker_.joinable()) worker_.join();
  }
private:
  void receive() {
    const auto now = Clock::now();
    std::lock_guard<std::mutex> lock(mutex_);
    if (stopping_) return;
    if (!started_) {
      started_ = true;
      start_ = now + std::chrono::seconds(warmup_);
      end_ = start_ + std::chrono::seconds(duration_);
      const auto epoch = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count() + warmup_ * 1000LL;
      std::printf("MEASURE_START_MS %lld\nMEASURE_END_MS %lld\n",
        static_cast<long long>(epoch), static_cast<long long>(epoch + duration_ * 1000LL));
      std::fflush(stdout);
      cv_.notify_all();
    }
    if (now >= start_ && now < end_) ++received_;
  }
  void finish_at_deadline() {
    std::unique_lock<std::mutex> lock(mutex_);
    const auto startup_deadline = Clock::now() + std::chrono::seconds(30);
    while (!stopping_ && rclcpp::ok()) {
      const auto now = Clock::now();
      if (started_ && now >= end_) {
        stopping_ = true;
        const auto expected = static_cast<uint64_t>(std::llround(hz_ * duration_));
        const double loss = 100.0 * (expected - std::min(expected, received_)) / expected;
        std::printf("TOPIC_RESULT topic=%s recv=%lu expected=%lu duration=%d\n",
          topic_.c_str(), received_, expected, duration_);
        std::printf("FINAL [%ds]: recv %lu / expected %lu -> drop %.6f%%\n",
          duration_, received_, expected, loss);
        std::fflush(stdout);
        lock.unlock();
        rclcpp::shutdown();
        return;
      }
      if (!started_ && now >= startup_deadline) {
        std::fprintf(stderr, "[ERROR] no first message within 30 seconds for %s\n", topic_.c_str());
        lock.unlock();
        rclcpp::shutdown();
        return; // No FINAL: the harness rejects a workload that never started.
      }
      cv_.wait_for(lock, std::chrono::milliseconds(20));
    }
  }
  typename rclcpp::Subscription<Message>::SharedPtr sub_;
  std::string topic_;
  double hz_;
  int warmup_, duration_;
  std::mutex mutex_;
  std::condition_variable cv_;
  std::thread worker_;
  bool started_ = false, stopping_ = false;
  uint64_t received_ = 0;
  Clock::time_point start_, end_;
};
} // namespace rp_exp
