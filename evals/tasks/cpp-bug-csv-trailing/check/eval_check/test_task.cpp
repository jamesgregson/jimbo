#include "ledger/csv.h"
#include "test.h"

using ledger::split_csv_line;

TEST(eval_trailing_empty_field) {
    auto f = split_csv_line("cash,$5.00,");
    CHECK_EQ(f.size(), 3u);
    CHECK_EQ(f[2], "");
}

TEST(eval_empty_quoted_and_middle_fields) {
    CHECK_EQ(split_csv_line(",").size(), 2u);
    CHECK_EQ(split_csv_line("a,,c").size(), 3u);
    auto f = split_csv_line("\"x\",\"\"");
    CHECK_EQ(f.size(), 2u);
    CHECK_EQ(f[0], "x");
}
