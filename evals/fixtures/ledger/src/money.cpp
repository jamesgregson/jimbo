#include "ledger/money.h"

#include <algorithm>
#include <iomanip>
#include <regex>
#include <sstream>
#include <stdexcept>

namespace ledger {

static std::string trim(const std::string& s) {
    auto start = s.find_first_not_of(" \t\r\n");
    if (start == std::string::npos) return "";
    auto end = s.find_last_not_of(" \t\r\n");
    return s.substr(start, end - start + 1);
}

long long parse_money(const std::string& text) {
    std::string s = trim(text);
    bool negative = !s.empty() && s[0] == '-';
    if (negative) s.erase(0, 1);
    if (!s.empty() && s[0] == '$') s.erase(0, 1);
    s.erase(std::remove(s.begin(), s.end(), ','), s.end());
    static const std::regex amount(R"(\d+(\.\d{1,2})?)");
    if (!std::regex_match(s, amount)) throw std::invalid_argument("not an amount: " + text);
    auto dot = s.find('.');
    std::string whole = s.substr(0, dot);
    std::string frac = dot == std::string::npos ? "" : s.substr(dot + 1);
    frac.resize(2, '0');
    long long cents = std::stoll(whole) * 100 + std::stoll(frac);
    return negative ? -cents : cents;
}

std::string format_money(long long cents) {
    long long whole = cents / 100;
    long long frac = cents % 100;
    std::string digits = std::to_string(whole);
    std::string grouped;
    for (size_t i = 0; i < digits.size(); ++i) {
        if (i > 0 && (digits.size() - i) % 3 == 0) grouped += ',';
        grouped += digits[i];
    }
    std::ostringstream out;
    out << (cents < 0 ? "-$" : "$") << grouped << '.' << std::setw(2) << std::setfill('0') << frac;
    return out.str();
}

}  // namespace ledger
