cmake_minimum_required(VERSION 3.8)
project(rp_exp_overhead)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic -O2)
endif()

include_directories("${CMAKE_CURRENT_SOURCE_DIR}/../../../common/include")

find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(sensor_msgs REQUIRED)

add_executable(stress_pub src/stress_pub.cpp)
ament_target_dependencies(stress_pub rclcpp sensor_msgs)

add_executable(stress_sub src/stress_sub.cpp)
ament_target_dependencies(stress_sub rclcpp sensor_msgs)

install(TARGETS stress_pub stress_sub DESTINATION lib/${PROJECT_NAME})

ament_package()
