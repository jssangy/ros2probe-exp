#include <chrono>
#include <memory>

#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/compressed_image.hpp>

#ifdef RELIABLE_QOS
static constexpr const char * QOS_LABEL = "RELIABLE";
#else
static constexpr const char * QOS_LABEL = "BEST_EFFORT";
#endif

static constexpr double   HZ              = 30.0;
static constexpr uint32_t JPEG_PAYLOAD_B  = 150 * 1024;  // ~150 KB (720p JPEG 품질 80% 중간값)

class S4aPublisher : public rclcpp::Node
{
public:
  explicit S4aPublisher(uint64_t max_count)
  : Node("s4a_publisher"), pub_count_(0), max_count_(max_count)
  {
    rclcpp::QoS qos(1);
#ifdef RELIABLE_QOS
    qos.reliable();
#else
    qos.best_effort();
#endif
    pub_ = create_publisher<sensor_msgs::msg::CompressedImage>("/image_raw/compressed", qos);

    // 메시지 초기화 시 한 번만 할당
    msg_ = std::make_shared<sensor_msgs::msg::CompressedImage>();
    msg_->header.frame_id = "camera";
    msg_->format          = "jpeg";

    // data 배열 초기화 후 재사용 (resize는 여기서만)
    // JPEG SOI 마커(FF D8 FF)로 시작해 포맷 식별 가능하게 유지
    msg_->data.resize(JPEG_PAYLOAD_B, 0x00);
    msg_->data[0] = 0xFF;
    msg_->data[1] = 0xD8;
    msg_->data[2] = 0xFF;

    auto period = std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::duration<double>(1.0 / HZ));
    timer_ = create_wall_timer(period, [this]() { timer_cb(); });

    RCLCPP_INFO(get_logger(),
      "S4-a publisher started: %.1f Hz, JPEG 720p ~%u KB, max %lu msgs, %s",
      HZ, JPEG_PAYLOAD_B / 1024, max_count_, QOS_LABEL);
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
    if (pub_count_ % 150 == 0) {
      RCLCPP_INFO(get_logger(), "published %lu msgs", pub_count_);
    }
  }

  rclcpp::Publisher<sensor_msgs::msg::CompressedImage>::SharedPtr pub_;
  rclcpp::TimerBase::SharedPtr timer_;
  sensor_msgs::msg::CompressedImage::SharedPtr msg_;
  uint64_t pub_count_;
  uint64_t max_count_;
};

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);
  auto args = rclcpp::remove_ros_arguments(argc, argv);

  uint64_t max_count = 0;  // 0 = infinite; subscriber controls measurement window
  if (args.size() > 1) {
    max_count = std::stoull(args[1]);
  }

  auto node = std::make_shared<S4aPublisher>(max_count);

  rclcpp::executors::MultiThreadedExecutor exec;
  exec.add_node(node);
  exec.spin();

  rclcpp::shutdown();
  return 0;
}
