#pragma once

#include <string>

namespace ledger {

// "$1,234.50" -> 123450 cents. Accepts a leading -, an optional $, commas, and 0 to 2 decimals.
// Throws std::invalid_argument for anything else.
long long parse_money(const std::string& text);

// 123450 -> "$1,234.50".
std::string format_money(long long cents);

}  // namespace ledger
