#pragma once

#include <string>
#include <vector>

namespace ledger {

// Split one CSV line into fields. Fields may be quoted with ", and "" inside quotes is one ".
std::vector<std::string> split_csv_line(const std::string& line);

}  // namespace ledger
