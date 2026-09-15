#include <chrono>   // 목표 시각·대기 시간 계산
#include <cstdlib>  // setenv, getenv, rand, srand
#include <fstream>
#include <iomanip>  // setprecision (time_epoch 출력 정밀도)
#include <iostream>
#include <algorithm>  // sort (토픽 열 순서 고정)
#include <memory>
#include <set>
#include <string>
#include <thread>   // sleep_until, sleep_for
#include <vector>
#include <atomic>   // atomic_thread_fence (재배치 방지)
#include <unordered_map>

#include "rcl/publisher.h"
#include "rcl/subscription.h"
#include "rclcpp/rclcpp.hpp"
#include "std_msgs/msg/string.hpp"

namespace
{
std::string local_topic_key(const std::string & topic)
{
    if (!topic.empty() && topic.front() == '/') {
        return topic.substr(1);
    }
    return topic;
}
}  // namespace

int main(int argc, char * argv[])
{
    // The controller sets the domain for both observer and participants.
    if (!std::getenv("ROS_DOMAIN_ID")) setenv("ROS_DOMAIN_ID", "0", 0);

    // 1. ROS 2 초기화
    rclcpp::init(argc, argv);

    // 파라미터: [엔드포인트 개수] [목표 시작 시각(Unix timestamp)] [실행 유지 시간(초), 기본 60]
    int num_endpoints = (argc > 1) ? std::stoi(argv[1]) : 2;

    if (argc < 3) {
        std::cerr << "Usage: discovery_test <num_endpoints> <target_start_time_unix_timestamp> [run_duration_sec]" << std::endl;
        std::cerr << "Example: discovery_test 50 $(date +%s -d '+30 seconds') 60" << std::endl;
        return 1;
    }

    int64_t target_timestamp = std::stoll(argv[2]);  // 목표 시작 시각 (마스터가 넘긴 Unix timestamp)
    int run_duration_sec = (argc > 3) ? std::stoi(argv[3]) : 60;
    // 0 = 분배(stress_topic_0,1,...), 1 = 한 토픽(stress_topic_), 2 = 랜덤(stress_topic_0..N_E 중 무작위)
    int topic_mode = (argc > 4) ? std::stoi(argv[4]) : 0;
    // launch에서 N_P개 띄울 때 구분용 노드 이름 접미사 (argv[5], 없으면 "0")
    std::string node_id = (argc > 5) ? argv[5] : "0";
    // 실험 전체에서 생성한 discovery_test 개수 (argv[6], 없으면 1)
    int total_processes = (argc > 6) ? std::stoi(argv[6]) : 1;
    std::string node_suffix = "_" + node_id;
    std::string node_name = "discovery_stress_node" + node_suffix;

    // 엔드포인트 개수에 따른 Publisher/Subscriber 분배: 짝수면 동일, 홀수면 Publisher가 더 많게
    int num_publishers = (num_endpoints + 1) / 2;  // 올림
    int num_subscribers = num_endpoints / 2;       // 내림

    // QoS: 코드에서 여기만 바꾸면 됨
    rclcpp::QoS qos(10);  // depth 10 (KEEP_LAST 일 때만 사용)
    qos.keep_all();       // KEEP_ALL: 샘플 전부 유지. KEEP_LAST 로 바꾸려면 이 줄 지우고 기본값 또는 qos.keep_last(10)
    qos.reliability(rclcpp::ReliabilityPolicy::Reliable);   // BestEffort 로 바꾸려면 ReliabilityPolicy::BestEffort
    qos.durability(rclcpp::DurabilityPolicy::Volatile);     // TransientLocal 로 바꾸려면 DurabilityPolicy::TransientLocal

    // 2. target_time 이전에 가능한 모든 사전 계산을 끝낸다 (DDS 작업은 일절 하지 않음).
    //    Participant·DataWriter·DataReader 생성은 sleep_until 이후로 옮겨야 PDP/EDP가
    //    target_time 이후에만 발생한다.
    if (topic_mode == 2) {
        unsigned seed = static_cast<unsigned>(std::chrono::system_clock::now().time_since_epoch().count());
        if (!node_id.empty()) seed += static_cast<unsigned>(1000 * (std::stoi(node_id) + 1));
        std::srand(seed);
    }

    auto choose_topic = [&](int i) -> std::string {
        if (topic_mode == 1) return "stress_topic_";
        if (topic_mode == 2) return "stress_topic_" + std::to_string(std::rand() % (num_endpoints + 1));
        return "stress_topic_" + std::to_string(i);
    };

    // 사전 계산: pub/sub의 토픽 이름과 토픽별 로컬 개수
    std::vector<std::string> pub_topics(num_publishers);
    std::vector<std::string> sub_topics(num_subscribers);
    std::set<std::string> topics_used;
    std::unordered_map<std::string, size_t> local_pub_per_topic;
    std::unordered_map<std::string, size_t> local_sub_per_topic;

    for (int i = 0; i < num_publishers; ++i) {
        std::string topic_name = choose_topic(i);
        pub_topics[i] = topic_name;
        topics_used.insert(topic_name);
        local_pub_per_topic[topic_name] += 1;
    }
    for (int i = 0; i < num_subscribers; ++i) {
        std::string topic_name = choose_topic(i);
        sub_topics[i] = topic_name;
        topics_used.insert(topic_name);
        local_sub_per_topic[topic_name] += 1;
    }

    std::vector<std::string> topics_sorted(topics_used.begin(), topics_used.end());
    std::sort(topics_sorted.begin(), topics_sorted.end());

    // report_dir에 토픽 목록과 CSV 헤더만 미리 기록 (DDS 작업 아님)
    const char * report_dir = std::getenv("DISCOVERY_TOPIC_REPORT_DIR");
    std::string csv_path;
    if (report_dir && *report_dir) {
        std::string base = std::string(report_dir) + "/node_" + node_id;
        std::ofstream ftxt(base + ".txt");
        if (ftxt) { for (const auto & t : topics_sorted) ftxt << t << "\n"; }
        csv_path = base + "_topic_endpoint_count.csv";
        std::ofstream fcsv(csv_path);
        if (fcsv) {
            fcsv << "time_epoch";
            for (const auto & t : topics_sorted) { fcsv << "," << t << "_pub," << t << "_sub"; }
            fcsv << ",total_pub,total_sub\n";
        }
    }

    std::cout << "[Step 1] Pre-computation done (no DDS yet). Waiting for target time..." << std::endl;

    // 3. 타이밍 동기화: 마스터가 지정한 절대 시각까지 대기. 이 시점까지 Participant도, Endpoint도 없다.
    auto target_time = std::chrono::system_clock::from_time_t(target_timestamp);
    auto now = std::chrono::system_clock::now();
    auto wait_duration = std::chrono::duration_cast<std::chrono::seconds>(
        target_time - now).count();

    std::cout << ">>> Target start time (Unix timestamp): " << target_timestamp << std::endl;
    std::cout << ">>> Current time (Unix timestamp): "
              << std::chrono::system_clock::to_time_t(now) << std::endl;
    std::cout << ">>> Waiting " << wait_duration << " s until target time..." << std::endl;

    std::this_thread::sleep_until(target_time);

    // 4. target_time 도달. 여기부터 DDS 디스커버리가 시작된다.
    std::atomic_thread_fence(std::memory_order_seq_cst);
    double start_epoch = std::chrono::duration<double>(
        std::chrono::system_clock::now().time_since_epoch()).count();
    std::cout << "[Step 2] target_time reached at epoch=" << std::fixed
              << std::setprecision(6) << start_epoch
              << ". Creating Participant and endpoints..." << std::endl;

    // Node(=DDS Participant) 생성 → PDP가 이 시점부터 announce 시작
    auto node = std::make_shared<rclcpp::Node>(node_name);

    // Publisher/Subscriber(=DataWriter/DataReader) 생성 → EDP가 이 시점부터 announce 시작
    std::vector<rclcpp::Publisher<std_msgs::msg::String>::SharedPtr> publishers;
    std::vector<rclcpp::Subscription<std_msgs::msg::String>::SharedPtr> subscribers;
    publishers.reserve(num_publishers);
    subscribers.reserve(num_subscribers);
    for (int i = 0; i < num_publishers; ++i) {
        publishers.push_back(node->create_publisher<std_msgs::msg::String>(pub_topics[i], qos));
    }
    for (int i = 0; i < num_subscribers; ++i) {
        subscribers.push_back(
            node->create_subscription<std_msgs::msg::String>(
                sub_topics[i],
                qos,
                [](const std_msgs::msg::String::SharedPtr /* msg */) {}
            )
        );
    }

    // 5. proxy 매칭 추적용 트랙 구성
    struct PublisherMatchTrack
    {
        std::string topic;
        std::shared_ptr<rcl_publisher_t> handle;
        size_t expected_matches;
    };
    struct SubscriptionMatchTrack
    {
        std::string topic;
        std::shared_ptr<rcl_subscription_t> handle;
        size_t expected_matches;
    };

    std::vector<PublisherMatchTrack> publisher_tracks;
    publisher_tracks.reserve(publishers.size());
    for (const auto & pub : publishers) {
        const std::string topic = pub->get_topic_name();
        const std::string topic_key = local_topic_key(topic);
        // rmw_publisher_count_matched_subscriptions는 같은 프로세스 내 subscriber도 포함해 카운트한다
        // (EDP::newLocalReaderProxyData → pairing → onWriterMatched 경로).
        // expected = 전체 프로세스 수 × 이 토픽의 로컬 구독 수 (자기 프로세스 sub 포함)
        size_t local_count = local_sub_per_topic[topic_key];
        size_t expected = static_cast<size_t>(total_processes) * local_count;
        publisher_tracks.push_back(PublisherMatchTrack{topic, pub->get_publisher_handle(), expected});
    }

    std::vector<SubscriptionMatchTrack> subscription_tracks;
    subscription_tracks.reserve(subscribers.size());
    for (const auto & sub : subscribers) {
        const std::string topic = sub->get_topic_name();
        const std::string topic_key = local_topic_key(topic);
        size_t local_count = local_pub_per_topic[topic_key];
        size_t expected = static_cast<size_t>(total_processes) * local_count;
        subscription_tracks.push_back(SubscriptionMatchTrack{topic, sub->get_subscription_handle(), expected});
    }

    // 6. 1ms polling: rmw_*_count_matched_*는 DDS 내부 스레드가 갱신하므로 executor 없이도 즉시 호출 가능.
    //    매칭된 proxy 수가 expected에 도달하는 첫 시점을 기록한다.
    auto polling_deadline = target_time + std::chrono::seconds(run_duration_sec);
    bool completion_recorded = false;
    if (report_dir && *report_dir) {
        while (rclcpp::ok() && std::chrono::system_clock::now() < polling_deadline) {
            bool all_matched = true;

            for (const auto & track : publisher_tracks) {
                size_t matched = 0;
                auto * rmw_handle = rcl_publisher_get_rmw_handle(track.handle.get());
                if (!rmw_handle ||
                    rmw_publisher_count_matched_subscriptions(rmw_handle, &matched) != RMW_RET_OK) {
                    rmw_reset_error();
                    all_matched = false;
                    break;
                }
                if (matched < track.expected_matches) {
                    all_matched = false;
                    break;
                }
            }
            if (all_matched) {
                for (const auto & track : subscription_tracks) {
                    size_t matched = 0;
                    auto * rmw_handle = rcl_subscription_get_rmw_handle(track.handle.get());
                    if (!rmw_handle ||
                        rmw_subscription_count_matched_publishers(rmw_handle, &matched) != RMW_RET_OK) {
                        rmw_reset_error();
                        all_matched = false;
                        break;
                    }
                    if (matched < track.expected_matches) {
                        all_matched = false;
                        break;
                    }
                }
            }

            if (all_matched) {
                double epoch = std::chrono::duration<double>(
                    std::chrono::system_clock::now().time_since_epoch()).count();
                size_t total_pub = 0, total_sub = 0;
                std::ofstream f(csv_path, std::ios::app);
                if (f) {
                    f << std::fixed << std::setprecision(6) << epoch;
                    for (const auto & topic : topics_sorted) {
                        size_t np = node->count_publishers(topic);
                        size_t ns = node->count_subscribers(topic);
                        total_pub += np;
                        total_sub += ns;
                        f << "," << np << "," << ns;
                    }
                    f << "," << total_pub << "," << total_sub << "\n";
                    f.flush();
                }
                completion_recorded = true;
                std::cout << "[Step 3] Matched: completion epoch=" << std::fixed
                          << std::setprecision(6) << epoch << std::endl;
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
        if (!completion_recorded) {
            std::cout << "[Step 3] Polling deadline reached without full match." << std::endl;
        }
    }

    // 7. run_duration_sec 가 끝날 때까지 process 유지 (다른 호스트의 폴링이 끝나도록).
    //    DDS 자체는 별도 스레드에서 동작하므로 executor 없이 sleep만으로도 충분하지만,
    //    rclcpp::shutdown() 처리를 깔끔히 하기 위해 executor를 띄워 둔다.
    rclcpp::executors::SingleThreadedExecutor executor;
    executor.add_node(node);
    std::thread spin_thread([&executor]() { executor.spin(); });

    auto remaining = polling_deadline - std::chrono::system_clock::now();
    if (remaining > std::chrono::milliseconds(0)) {
        std::this_thread::sleep_for(remaining);
    }
    rclcpp::shutdown();
    if (spin_thread.joinable()) spin_thread.join();

    return 0;
}
