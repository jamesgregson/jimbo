#include "ledger/ledger.h"
#include "test.h"

TEST(eval_balance_of) {
    ledger::Ledger l;
    l.add({"cash", 100, ""});
    l.add({"cash", 23, ""});
    const ledger::Ledger& c = l;
    CHECK_EQ(c.balance_of("cash"), 123);
}
