#include "ledger/money.h"
#include "test.h"

using ledger::parse_money;

TEST(parses_amounts) {
    CHECK_EQ(parse_money("$12"), 1200);
    CHECK_EQ(parse_money("$1,234.50"), 123450);
    CHECK_EQ(parse_money("1.5"), 150);
    CHECK_EQ(parse_money("-$3.07"), -307);
}

TEST(rejects_invalid) {
    CHECK_THROWS(parse_money(""));
    CHECK_THROWS(parse_money("abc"));
    CHECK_THROWS(parse_money("1.234"));
    CHECK_THROWS(parse_money("$"));
}
