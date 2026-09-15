cmake_minimum_required(VERSION 3.8)
project(rp_exp_fidelity)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic -O2)
endif()

find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(sensor_msgs REQUIRED)

add_executable(drop_image_pub src/drop_image_pub.cpp)
ament_target_dependencies(drop_image_pub rclcpp sensor_msgs)

add_executable(drop_image_sub src/drop_image_sub.cpp)
ament_target_dependencies(drop_image_sub rclcpp sensor_msgs)

install(TARGETS drop_image_pub drop_image_sub DESTINATION lib/${PROJECT_NAME})

ament_package()
