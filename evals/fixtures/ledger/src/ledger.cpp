#include "ledger/ledger.h"

#include <fstream>
#include <stdexcept>

#include "ledger/csv.h"
#include "ledger/money.h"

namespace ledger {

void Ledger::add(Entry entry) {
    entries_.push_back(std::move(entry));
}

long long Ledger::balance(const std::string& account) const {
    long long total = 0;
    for (const auto& e : entries_) {
        if (e.account == account) total += e.cents;
    }
    return total;
}

std::vector<Entry> Ledger::entries_for(const std::string& account) const {
    std::vector<Entry> out;
    for (const auto& e : entries_) {
        if (e.account == account) out.push_back(e);
    }
    return out;
}

void Ledger::remove_account(const std::string& account) {
    for (size_t i = 0; i < entries_.size(); ++i) {
        if (entries_[i].account == account) entries_.erase(entries_.begin() + i);
    }
}

Ledger load_ledger(const std::string& path) {
    std::ifstream in(path);
    if (!in) throw std::runtime_error("can't open " + path);
    Ledger ledger;
    std::string line;
    std::getline(in, line);  // header
    int number = 1;
    while (std::getline(in, line)) {
        ++number;
        if (!line.empty() && line.back() == '\r') line.pop_back();
        auto fields = split_csv_line(line);
        if (fields.size() != 3) throw std::runtime_error(path + ":" + std::to_string(number) + ": expected 3 fields");
        ledger.add({fields[0], parse_money(fields[1]), fields[2]});
    }
    return ledger;
}

}  // namespace ledger
