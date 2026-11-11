#include "ledger/ledger.h"
#include "test.h"

using ledger::Ledger;

TEST(balance_sums_one_account) {
    Ledger l;
    l.add({"cash", 1000, "in"});
    l.add({"savings", 500, "in"});
    l.add({"cash", -250, "out"});
    CHECK_EQ(l.balance_of("cash"), 750);
    CHECK_EQ(l.balance_of("savings"), 500);
    CHECK_EQ(l.balance_of("none"), 0);
}

TEST(loads_csv) {
    Ledger l = ledger::load_ledger("data/ledger.csv");
    CHECK_EQ(l.entries().size(), 4u);
    CHECK_EQ(l.balance_of("cash"), 8443);
    CHECK_EQ(l.entries_for("cash")[2].memo, "coffee, large");
}
