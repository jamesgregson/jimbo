#include <exception>

#include "test.h"

int main() {
    int failed = 0;
    for (const auto& c : test::cases()) {
        int before = test::failures;
        try {
            c.fn();
        } catch (const std::exception& e) {
            std::cerr << c.name << ": threw " << e.what() << "\n";
            ++test::failures;
        }
        bool ok = test::failures == before;
        failed += !ok;
        std::cout << (ok ? "ok   " : "FAIL ") << c.name << "\n";
    }
    std::cout << test::cases().size() - failed << " passed, " << failed << " failed\n";
    return failed ? 1 : 0;
}
