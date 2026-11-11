#include <map>
#include <string>

#include "ledger/ledger.h"
#include "test.h"

using ledger::Ledger;

TEST(eval_balances) {
    Ledger l;
    l.add({"cash", 1000, ""});
    l.add({"savings", 500, ""});
    l.add({"cash", -250, ""});
    l.add({"loan", -700, ""});
    const Ledger& c = l;
    std::map<std::string, long long> b = c.balances();
    CHECK_EQ(b.size(), 3u);
    CHECK_EQ(b["cash"], 750);
    CHECK_EQ(b["savings"], 500);
    CHECK_EQ(b["loan"], -700);
}

TEST(eval_balances_empty) {
    CHECK(Ledger().balances().empty());
}
