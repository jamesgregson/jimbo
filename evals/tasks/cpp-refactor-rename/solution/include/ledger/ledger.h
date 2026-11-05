#pragma once

#include <string>
#include <vector>

namespace ledger {

struct Entry {
    std::string account;
    long long cents;
    std::string memo;
};

class Ledger {
public:
    void add(Entry entry);
    long long balance_of(const std::string& account) const;
    std::vector<Entry> entries_for(const std::string& account) const;
    void remove_account(const std::string& account);
    const std::vector<Entry>& entries() const { return entries_; }

private:
    std::vector<Entry> entries_;
};

// Read a CSV file with a header line and account, amount and memo columns. Amounts use parse_money.
// Throws std::runtime_error if the file can't be opened or a line has the wrong number of fields.
Ledger load_ledger(const std::string& path);

}  // namespace ledger
