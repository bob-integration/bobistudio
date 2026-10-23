# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
CFLAGS = -O2 -g -Wall -Wno-unused-result -std=gnu11
all: libplugcore.so host engine
libplugcore.so: plugcore.c
	$(CC) $(CFLAGS) -fPIC -shared -o $@ $< $$(pkg-config --cflags --libs lilv-0) -lm
host: host.c shm.h libplugcore.so
	$(CC) $(CFLAGS) -o $@ host.c -L. -lplugcore -Wl,-rpath,'$$ORIGIN' -lrt
engine: engine.c shm.h
	$(CC) $(CFLAGS) -o $@ engine.c -ldl -lpthread -lrt -lm
