#include <iostream>
#include <vector>
#include <string>
#include <cstring>
#include <unistd.h>
#include <arpa/inet.h>
#include <chrono>
#include <thread>
#include <fstream>
#include <regex>

using namespace std;

// --- 설정 ---
const string INTERFACE = "wlp2s0";
const char* TARGET_IP = "192.168.0.3"; // 수신측 IP
const int PORT = 5005;
const double BANDWIDTH_MBPS = 150.0;
const double SAFETY_FACTOR = 0.1; // 0.1 ~ 0.7 사이에서 조절
const int DURATION_SEC = 10;
const vector<int> FREQ_HZ = {10, 20, 50, 100};  // 고정된 여러 Hz (초당 메시지 수)
const vector<int> SIZES = {200, 600, 1000, 1400};

// iw survey dump에서 busy time을 가져오는 함수
long long get_survey_busy() {
    string cmd = "iw dev " + INTERFACE + " survey dump";
    char buffer[128];
    string result = "";
    FILE* pipe = popen(cmd.c_str(), "r");
    if (!pipe) return 0;
    while (fgets(buffer, sizeof(buffer), pipe) != NULL) result += buffer;
    pclose(pipe);

    // 가장 마지막 "channel busy time" 매칭
    regex re("channel busy time:\\s+(\\d+) ms");
    smatch match;
    long long last_busy = 0;
    string::const_search_iterator searchStart(result.cbegin());
    while (regex_search(searchStart, result.cend(), match, re)) {
        last_busy = stoll(match[1]);
        searchStart = match.suffix().first;
    }
    return last_busy;
}

int main() {
    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    sockaddr_in servaddr;
    memset(&servaddr, 0, sizeof(servaddr));
    servaddr.sin_family = AF_INET;
    servaddr.sin_port = htons(PORT);
    inet_pton(AF_INET, TARGET_IP, &servaddr.sin_addr);

    struct Result { int freq_hz; int bits; double avg_airtime; };
    vector<Result> final_results;

    cout << "--- 실험 시작 (고정 Hz: ";
    for (size_t i = 0; i < FREQ_HZ.size(); i++) cout << (i ? ", " : "") << FREQ_HZ[i];
    cout << " Hz, 메시지 크기: ";
    for (size_t i = 0; i < SIZES.size(); i++) cout << (i ? ", " : "") << SIZES[i] << "B";
    cout << ") ---" << endl;

    for (int freq_hz : FREQ_HZ) {
        int pps = (freq_hz < 1) ? 1 : freq_hz;

        for (int size : SIZES) {
            char* payload = new char[size];
            memset(payload, 'x', size);

            long long start_busy = get_survey_busy();
            auto start_time = chrono::steady_clock::now();
            auto end_time = start_time + chrono::seconds(DURATION_SEC);

            int count = 0;
            double interval_us = 1000000.0 / pps;

            cout << "[테스트] " << freq_hz << " Hz, Size: " << size << " B (PPS=" << pps << ")..." << endl;

            while (chrono::steady_clock::now() < end_time) {
                sendto(sock, payload, size, 0, (const struct sockaddr*)&servaddr, sizeof(servaddr));
                count++;
                auto next_tick = start_time + chrono::microseconds((long long)(count * interval_us));
                this_thread::sleep_until(next_tick);
            }

            long long end_busy = get_survey_busy();
            delete[] payload;

            double delta_busy_ms = (double)(end_busy - start_busy);
            double avg_airtime = (count > 0) ? (delta_busy_ms / count) : 0;

            final_results.push_back({freq_hz, size * 8, avg_airtime});
            cout << " >> Sent: " << count << ", Busy Δ: " << delta_busy_ms << " ms, Airtime/Pkt: " << avg_airtime << " ms" << endl;
        }
    }

    // --- Hz별 선형 회귀 ---
    cout << "\n========================================" << endl;
    cout << "📊 Hz별 추정 결과" << endl;
    for (int freq_hz : FREQ_HZ) {
        double sum_x = 0, sum_y = 0, sum_xx = 0, sum_xy = 0;
        int n = 0;
        for (const auto& r : final_results) {
            if (r.freq_hz != freq_hz) continue;
            n++;
            sum_x += r.bits;
            sum_y += r.avg_airtime;
            sum_xx += (double)r.bits * r.bits;
            sum_xy += (double)r.bits * r.avg_airtime;
        }
        if (n < 2) continue;
        double slope = (n * sum_xy - sum_x * sum_y) / (n * sum_xx - sum_x * sum_x);
        double intercept = (sum_y - slope * sum_x) / n;
        double R_eff = (slope != 0) ? (1.0 / slope) * 1000.0 : 0; // bits/ms -> bps
        cout << "  " << freq_hz << " Hz -> R: " << (R_eff / 1e6) << " Mbps, 오버헤드: " << intercept << " ms" << endl;
    }
    cout << "========================================" << endl;

    close(sock);
    return 0;
}