/* SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026 BOBI SAS, France
 *
 * What a HOSTILE third-party plugin would try, shared by the CLAP and VST3 test plugins (tests/data/clap_test.c,
 * vst3_test.c) — the same attempts as the host's built-in `escape` plugin (host/src/builtin.rs). Each result is
 * 1 when the attempt was REFUSED (what the sandbox must give), 0 when it succeeded. Test fixture only: never
 * installed anywhere but in a temporary folder of the test.
 */
#ifndef BOBI_ESCAPE_PROBES_H
#define BOBI_ESCAPE_PROBES_H
#define _GNU_SOURCE
#include <fcntl.h>
#include <signal.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

#define ESC_N 10
static const char *const ESC_NAMES[ESC_N] = {
    "open /dev/shm/mxl refused", "open /proc/1/environ refused", "read /etc/passwd refused", "create /tmp file refused",
    "MXL_AGENT_TOKEN absent", "socket(AF_INET) refused", "socket(AF_UNIX) refused", "fork refused", "execve refused",
    "signal to the parent refused"};

static int esc_refused_open(const char *p, int flags) {
    int fd = open(p, flags | O_CLOEXEC, 0600);
    if (fd >= 0) {
        close(fd);
        return 0;
    }
    return 1;
}

static void escape_probes(double out[ESC_N]) {
    out[0] = esc_refused_open("/dev/shm/mxl", O_RDONLY | O_DIRECTORY);
    out[1] = esc_refused_open("/proc/1/environ", O_RDONLY);
    out[2] = esc_refused_open("/etc/passwd", O_RDONLY);
    out[3] = esc_refused_open("/tmp/bobi_plugin_escape_probe", O_WRONLY | O_CREAT);
    out[4] = getenv("MXL_AGENT_TOKEN") == NULL;
    int s = socket(AF_INET, SOCK_DGRAM, 0);
    out[5] = s < 0;
    if (s >= 0) close(s);
    s = socket(AF_UNIX, SOCK_STREAM, 0);
    out[6] = s < 0;
    if (s >= 0) close(s);
    pid_t pid = fork();
    if (pid == 0) _exit(0);
    if (pid > 0) waitpid(pid, NULL, 0);
    out[7] = pid < 0;
    char *const argv[] = {(char *)"/bin/true", NULL};
    char *const envp[] = {NULL};
    out[8] = execve("/bin/true", argv, envp) < 0; /* returns only on failure */
    out[9] = kill(getppid(), 0) != 0;
}
#endif
