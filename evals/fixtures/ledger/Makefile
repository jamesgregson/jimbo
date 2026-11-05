CXX ?= c++
CXXFLAGS ?= -std=c++17 -Wall -Wextra -g
CPPFLAGS += -Iinclude -Itests

SRC := $(wildcard src/*.cpp)
TESTS := $(wildcard tests/*.cpp)
HEADERS := $(wildcard include/ledger/*.h tests/*.h)

test: build/tests
	./build/tests

build/tests: $(SRC) $(TESTS) $(HEADERS)
	mkdir -p build
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -o $@ $(SRC) $(TESTS)

clean:
	rm -rf build

.PHONY: test clean
