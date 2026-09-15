cmake_minimum_required(VERSION 3.8)
project(rp_exp_probe_effect)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic -O2)
endif()

include_directories("${CMAKE_CURRENT_SOURCE_DIR}/../../../common/include")

find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(sensor_msgs REQUIRED)

set(DEPS rclcpp geometry_msgs sensor_msgs)

function(add_reliable_executable target source)
  add_executable(${target} ${source})
  target_compile_definitions(${target} PRIVATE RELIABLE_QOS=1)
  ament_target_dependencies(${target} ${DEPS})
endfunction()

# S1 — /cmd_vel (geometry_msgs/Twist)
add_executable(s1_pub src/s1_pub.cpp)
ament_target_dependencies(s1_pub ${DEPS})
add_reliable_executable(s1_pub_rel src/s1_pub.cpp)

add_executable(s1_sub src/s1_sub.cpp)
ament_target_dependencies(s1_sub ${DEPS})
add_reliable_executable(s1_sub_rel src/s1_sub.cpp)

# S2 — /imu (sensor_msgs/Imu)
add_executable(s2_pub src/s2_pub.cpp)
ament_target_dependencies(s2_pub ${DEPS})
add_reliable_executable(s2_pub_rel src/s2_pub.cpp)

add_executable(s2_sub src/s2_sub.cpp)
ament_target_dependencies(s2_sub ${DEPS})
add_reliable_executable(s2_sub_rel src/s2_sub.cpp)

# S3-a — /scan (sensor_msgs/LaserScan)
add_executable(s3a_pub src/s3a_pub.cpp)
ament_target_dependencies(s3a_pub ${DEPS})
add_reliable_executable(s3a_pub_rel src/s3a_pub.cpp)

add_executable(s3a_sub src/s3a_sub.cpp)
ament_target_dependencies(s3a_sub ${DEPS})
add_reliable_executable(s3a_sub_rel src/s3a_sub.cpp)

# S3-b/c — /points (sensor_msgs/PointCloud2, num_points 인자로 구분)
add_executable(s3_points_pub src/s3_points_pub.cpp)
ament_target_dependencies(s3_points_pub ${DEPS})
add_reliable_executable(s3_points_pub_rel src/s3_points_pub.cpp)

add_executable(s3_points_sub src/s3_points_sub.cpp)
ament_target_dependencies(s3_points_sub ${DEPS})
add_reliable_executable(s3_points_sub_rel src/s3_points_sub.cpp)

# S4-a — /image_raw/compressed (sensor_msgs/CompressedImage)
add_executable(s4a_pub src/s4a_pub.cpp)
ament_target_dependencies(s4a_pub ${DEPS})
add_reliable_executable(s4a_pub_rel src/s4a_pub.cpp)

add_executable(s4a_sub src/s4a_sub.cpp)
ament_target_dependencies(s4a_sub ${DEPS})
add_reliable_executable(s4a_sub_rel src/s4a_sub.cpp)

# S4-b — /depth/image_raw (sensor_msgs/Image)
add_executable(s4_image_pub src/s4_image_pub.cpp)
ament_target_dependencies(s4_image_pub ${DEPS})
add_reliable_executable(s4_image_pub_rel src/s4_image_pub.cpp)

add_executable(s4_image_sub src/s4_image_sub.cpp)
ament_target_dependencies(s4_image_sub ${DEPS})
add_reliable_executable(s4_image_sub_rel src/s4_image_sub.cpp)

install(TARGETS
  s1_pub
  s1_pub_rel
  s1_sub
  s1_sub_rel
  s2_pub
  s2_pub_rel
  s2_sub
  s2_sub_rel
  s3a_pub
  s3a_pub_rel
  s3a_sub
  s3a_sub_rel
  s3_points_pub
  s3_points_pub_rel
  s3_points_sub
  s3_points_sub_rel
  s4a_pub
  s4a_pub_rel
  s4a_sub
  s4a_sub_rel
  s4_image_pub
  s4_image_pub_rel
  s4_image_sub
  s4_image_sub_rel
  DESTINATION lib/${PROJECT_NAME}
)

install(DIRECTORY launch
  DESTINATION share/${PROJECT_NAME}
)

ament_package()
