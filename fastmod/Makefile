CC ?= aarch64-linux-android33-clang
CFLAGS ?= -O2 -Wall -Wextra -pthread -static

fastmod: fastmod.c
	$(CC) $(CFLAGS) -o $@ $<

clean:
	rm -f fastmod
