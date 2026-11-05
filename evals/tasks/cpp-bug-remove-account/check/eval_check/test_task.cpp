#include "ledger/ledger.h"
#include "test.h"

using ledger::Ledger;

TEST(eval_removes_adjacent_entries) {
    Ledger l;
    l.add({"cash", 1, "a"});
    l.add({"cash", 2, "b"});
    l.add({"savings", 3, "c"});
    l.add({"cash", 4, "d"});
    l.add({"cash", 5, "e"});
    l.remove_account("cash");
    CHECK_EQ(l.entries().size(), 1u);
    CHECK_EQ(l.entries()[0].account, "savings");
    CHECK_EQ(l.balance("cash"), 0);
}

TEST(eval_keeps_order_of_others) {
    Ledger l;
    l.add({"a", 1, ""});
    l.add({"x", 2, ""});
    l.add({"b", 3, ""});
    l.add({"x", 4, ""});
    l.add({"c", 5, ""});
    l.remove_account("x");
    CHECK_EQ(l.entries().size(), 3u);
    CHECK_EQ(l.entries()[1].account, "b");
    CHECK_EQ(l.entries()[2].account, "c");
}
