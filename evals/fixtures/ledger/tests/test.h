#pragma once

// A minimal test harness. TEST(name) { ... } defines a test; tests/main.cpp runs them all.

#include <iostream>
#include <vector>

namespace test {

struct Case {
    const char* name;
    void (*fn)();
};

inline std::vector<Case>& cases() {
    static std::vector<Case> all;
    return all;
}

inline int failures = 0;

struct Register {
    Register(const char* name, void (*fn)()) { cases().push_back({name, fn}); }
};

}  // namespace test

#define TEST(name)                                      \
    static void name();                                 \
    static test::Register name##_register(#name, name); \
    static void name()

#define CHECK(cond)                                                                    \
    do {                                                                               \
        if (!(cond)) {                                                                 \
            std::cerr << __FILE__ << ":" << __LINE__ << ": CHECK(" #cond ") failed\n"; \
            ++test::failures;                                                          \
        }                                                                              \
    } while (0)

#define CHECK_EQ(a, b)                                                                        \
    do {                                                                                      \
        const auto check_a = (a);                                                             \
        const auto check_b = (b);                                                             \
        if (!(check_a == check_b)) {                                                          \
            std::cerr << __FILE__ << ":" << __LINE__ << ": CHECK_EQ(" #a ", " #b ") failed: " \
                      << check_a << " != " << check_b << "\n";                                \
            ++test::failures;                                                                 \
        }                                                                                     \
    } while (0)

#define CHECK_THROWS(expr)                                                                    \
    do {                                                                                      \
        bool check_thrown = false;                                                            \
        try {                                                                                 \
            (void)(expr);                                                                     \
        } catch (...) {                                                                       \
            check_thrown = true;                                                              \
        }                                                                                     \
        if (!check_thrown) {                                                                  \
            std::cerr << __FILE__ << ":" << __LINE__ << ": CHECK_THROWS(" #expr ") failed\n"; \
            ++test::failures;                                                                 \
        }                                                                                     \
    } while (0)
