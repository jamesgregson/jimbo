#include "ledger/csv.h"
#include "test.h"

using ledger::split_csv_line;

TEST(splits_plain_fields) {
    auto f = split_csv_line("cash,$1.00,lunch");
    CHECK_EQ(f.size(), 3u);
    CHECK_EQ(f[2], "lunch");
}

TEST(keeps_commas_in_quotes) {
    auto f = split_csv_line("cash,\"$1,000.00\",\"say \"\"hi\"\"\"");
    CHECK_EQ(f.size(), 3u);
    CHECK_EQ(f[1], "$1,000.00");
    CHECK_EQ(f[2], "say \"hi\"");
}
