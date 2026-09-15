#include <chrono>
#include <memory>
#include <stdexcept>
#include <string>

#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/image.hpp>

#ifdef RELIABLE_QOS
static constexpr const char * QOS_LABEL = "RELIABLE";
#else
static constexpr const char * QOS_LABEL = "BEST_EFFORT";
#endif

static constexpr double HZ = 30.0;

// encoding → bytes per pixel
static uint32_t step_from_encoding(const std::string & enc)
{
  if (enc == "rgb8" || enc == "bgr8") return 3;
  if (enc == "16UC1")                 return 2;
  if (enc == "8UC1" || enc == "mono8") return 1;
  throw std::invalid_argument("unsupported encoding: " + enc);
}

class S4ImagePublisher : public rclcpp::Node
{
public:
  S4ImagePublisher(uint32_t width, uint32_t height, const std::string & encoding,
                   uint64_t max_count)
  : Node("s4_image_publisher"), pub_count_(0), max_count_(max_count)
  {
    rclcpp::QoS qos(1);
#ifdef RELIABLE_QOS
    qos.reliable();
#else
    qos.best_effort();
#endif

    std::string topic = (encoding == "16UC1") ? "/depth/image_raw" : "/image_raw";
    pub_ = create_publisher<sensor_msgs::msg::Image>(topic, qos);

    uint32_t bpp      = step_from_encoding(encoding);
    uint32_t row_step = width * bpp;

    // 메시지 초기화 시 한 번만 할당
    msg_ = std::make_shared<sensor_msgs::msg::Image>();
    msg_->header.frame_id = "camera";
    msg_->width           = width;
    msg_->height          = height;
    msg_->encoding        = encoding;
    msg_->is_bigendian    = false;
    msg_->step            = row_step;

    // data 배열 초기화 후 재사용 (resize는 여기서만)
    msg_->data.resize(height * row_step, 0);

    double payload_mb = static_cast<double>(height * row_step) / (1024.0 * 1024.0);
    RCLCPP_INFO(get_logger(),
      "S4 image publisher started: %s %ux%u, %.2f MB/msg, %.1f Hz, max %lu msgs, %s",
      encoding.c_str(), width, height, payload_mb, HZ, max_count_, QOS_LABEL);

    auto period = std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::duration<double>(1.0 / HZ));
    timer_ = create_wall_timer(period, [this]() { timer_cb(); });
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

  rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr pub_;
  rclcpp::TimerBase::SharedPtr timer_;
  sensor_msgs::msg::Image::SharedPtr msg_;
  uint64_t pub_count_;
  uint64_t max_count_;
};

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);
  auto args = rclcpp::remove_ros_arguments(argc, argv);

  // 기본값: S4-b (depth, 16UC1, 640x480)
  uint32_t    width     = 640;
  uint32_t    height    = 480;
  std::string encoding  = "16UC1";
  uint64_t    max_count = 0;  // 0 = infinite; subscriber controls measurement window

  if (args.size() == 4 || args.size() == 5) {
    width    = static_cast<uint32_t>(std::stoul(args[1]));
    height   = static_cast<uint32_t>(std::stoul(args[2]));
    encoding = args[3];
    if (args.size() == 5) {
      max_count = std::stoull(args[4]);
    }
  } else if (args.size() == 2) {
    max_count = std::stoull(args[1]);
  } else if (args.size() != 1) {
    RCLCPP_ERROR(rclcpp::get_logger("s4_image_pub"),
      "usage: s4_image_pub [width height encoding [max_count]]");
    RCLCPP_ERROR(rclcpp::get_logger("s4_image_pub"),
      "  S4-b: 640  480  16UC1   (~614 KB)");
    return 1;
  }

  auto node = std::make_shared<S4ImagePublisher>(width, height, encoding, max_count);

  rclcpp::executors::MultiThreadedExecutor exec;
  exec.add_node(node);
  exec.spin();

  rclcpp::shutdown();
  return 0;
}
