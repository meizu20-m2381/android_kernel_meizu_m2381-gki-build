#define _GNU_SOURCE
#include <elf.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#ifndef __NR_finit_module
#define __NR_finit_module 273
#endif

#define MAX_MODS 1024
#define MAX_EDGES 8192

struct mod_info {
    char file[256];
    char path[512];
    char name[128];
    char *depends;
    char *softdep_pre;
    int idx;
    int in_deg;
    int loaded;
    int failed;
    int err;
    double elapsed_ms;
    int head_edge;
};

struct edge {
    int to;
    int next;
};

static struct mod_info mods[MAX_MODS];
static int n_mods = 0;
static struct edge edges[MAX_EDGES];
static int n_edges = 0;

static atomic_int parse_cursor = 0;
static pthread_mutex_t q_mtx = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t q_cv = PTHREAD_COND_INITIALIZER;
static int ready_q[MAX_MODS];
static int q_head = 0, q_tail = 0;
static int remaining = 0;
static int active_workers = 0;

static double now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000.0 + ts.tv_nsec / 1e6;
}

static void norm_name(const char *src, char *dst, size_t sz) {
    size_t i = 0;
    for (; src[i] && i + 1 < sz; i++) {
        dst[i] = (src[i] == '-') ? '_' : src[i];
    }
    dst[i] = '\0';
}

static int find_mod_by_name(const char *name) {
    for (int i = 0; i < n_mods; i++) {
        if (strcmp(mods[i].name, name) == 0)
            return i;
    }
    return -1;
}

static void add_edge(int from, int to) {
    if (from == to || from < 0 || to < 0) return;
    for (int e = mods[from].head_edge; e != -1; e = edges[e].next) {
        if (edges[e].to == to) return;
    }
    if (n_edges >= MAX_EDGES) return;
    edges[n_edges].to = to;
    edges[n_edges].next = mods[from].head_edge;
    mods[from].head_edge = n_edges++;
    mods[to].in_deg++;
}

static void parse_elf_modinfo(int idx) {
    int fd = open(mods[idx].path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return;
    struct stat st;
    if (fstat(fd, &st) < 0 || st.st_size < (off_t)sizeof(Elf64_Ehdr)) {
        close(fd);
        return;
    }
    uint8_t *base = mmap(NULL, st.st_size, PROT_READ, MAP_PRIVATE, fd, 0);
    close(fd);
    if (base == MAP_FAILED) return;

    Elf64_Ehdr *eh = (Elf64_Ehdr *)base;
    if (memcmp(eh->e_ident, ELFMAG, SELFMAG) != 0 || eh->e_ident[EI_CLASS] != ELFCLASS64 ||
        eh->e_shoff == 0 || eh->e_shnum == 0 || eh->e_shstrndx >= eh->e_shnum ||
        eh->e_shoff + (uint64_t)eh->e_shnum * sizeof(Elf64_Shdr) > (uint64_t)st.st_size) {
        munmap(base, st.st_size);
        return;
    }
    Elf64_Shdr *sh = (Elf64_Shdr *)(base + eh->e_shoff);
    Elf64_Shdr *shstr = &sh[eh->e_shstrndx];
    if (shstr->sh_offset + shstr->sh_size > (uint64_t)st.st_size) {
        munmap(base, st.st_size);
        return;
    }
    const char *strtab = (const char *)(base + shstr->sh_offset);

    const char *mi = NULL;
    size_t mi_sz = 0;
    for (int i = 0; i < eh->e_shnum; i++) {
        if (sh[i].sh_name < shstr->sh_size &&
            strcmp(strtab + sh[i].sh_name, ".modinfo") == 0 &&
            sh[i].sh_offset + sh[i].sh_size <= (uint64_t)st.st_size) {
            mi = (const char *)(base + sh[i].sh_offset);
            mi_sz = sh[i].sh_size;
            break;
        }
    }
    if (!mi) {
        munmap(base, st.st_size);
        return;
    }

    size_t pos = 0;
    while (pos < mi_sz) {
        if (mi[pos] == '\0') { pos++; continue; }
        const char *entry = mi + pos;
        size_t len = strnlen(entry, mi_sz - pos);
        if (strncmp(entry, "name=", 5) == 0 && len > 5) {
            norm_name(entry + 5, mods[idx].name, sizeof(mods[idx].name));
        } else if (strncmp(entry, "depends=", 8) == 0 && len > 8) {
            free(mods[idx].depends);
            mods[idx].depends = strndup(entry + 8, len - 8);
        } else if (strncmp(entry, "softdep=", 8) == 0 && len > 8) {
            const char *p = strstr(entry + 8, "pre:");
            if (p) {
                p += 4;
                size_t rlen = (entry + len) - p;
                char *tmp = strndup(p, rlen);
                if (tmp) {
                    char *post = strstr(tmp, "post:");
                    if (post) *post = '\0';
                    free(mods[idx].softdep_pre);
                    mods[idx].softdep_pre = tmp;
                }
            }
        }
        pos += len + 1;
    }
    munmap(base, st.st_size);
}

static void *parse_worker(void *arg) {
    (void)arg;
    for (;;) {
        int i = atomic_fetch_add(&parse_cursor, 1);
        if (i >= n_mods) break;
        parse_elf_modinfo(i);
    }
    return NULL;
}

static void build_edges(void) {
    for (int i = 0; i < n_mods; i++) {
        if (mods[i].depends) {
            char *save = NULL;
            for (char *tok = strtok_r(mods[i].depends, ",", &save); tok; tok = strtok_r(NULL, ",", &save)) {
                char dep[128];
                norm_name(tok, dep, sizeof(dep));
                int dep_idx = find_mod_by_name(dep);
                if (dep_idx >= 0) add_edge(dep_idx, i);
            }
        }
        if (mods[i].softdep_pre) {
            char *save = NULL;
            for (char *tok = strtok_r(mods[i].softdep_pre, " \t", &save); tok; tok = strtok_r(NULL, " \t", &save)) {
                char dep[128];
                norm_name(tok, dep, sizeof(dep));
                int dep_idx = find_mod_by_name(dep);
                if (dep_idx >= 0) add_edge(dep_idx, i);
            }
        }
    }
}

static int is_loaded_sysfs(const char *name) {
    char p[256];
    snprintf(p, sizeof(p), "/sys/module/%s", name);
    struct stat st;
    return stat(p, &st) == 0;
}

static void load_one(int idx) {
    double t0 = now_ms();
    if (is_loaded_sysfs(mods[idx].name)) {
        mods[idx].loaded = 1;
        mods[idx].elapsed_ms = now_ms() - t0;
        return;
    }
    int fd = open(mods[idx].path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        mods[idx].failed = 1;
        mods[idx].err = errno;
        mods[idx].elapsed_ms = now_ms() - t0;
        return;
    }
    long ret = syscall(__NR_finit_module, fd, "", 0);
    int err = errno;
    close(fd);
    mods[idx].elapsed_ms = now_ms() - t0;
    if (ret == 0 || err == EEXIST || is_loaded_sysfs(mods[idx].name)) {
        mods[idx].loaded = 1;
    } else {
        mods[idx].failed = 1;
        mods[idx].err = err;
    }
}

static void *worker_fn(void *arg) {
    (void)arg;
    for (;;) {
        pthread_mutex_lock(&q_mtx);
        while (q_head == q_tail && remaining > 0) {
            if (active_workers == 0) {
                pthread_mutex_unlock(&q_mtx);
                return NULL;
            }
            pthread_cond_wait(&q_cv, &q_mtx);
        }
        if (q_head == q_tail) {
            pthread_mutex_unlock(&q_mtx);
            return NULL;
        }
        int idx = ready_q[q_head++];
        active_workers++;
        pthread_mutex_unlock(&q_mtx);

        load_one(idx);

        pthread_mutex_lock(&q_mtx);
        active_workers--;
        remaining--;
        for (int e = mods[idx].head_edge; e != -1; e = edges[e].next) {
            int child = edges[e].to;
            if (--mods[child].in_deg == 0) {
                ready_q[q_tail++] = child;
            }
        }
        pthread_cond_broadcast(&q_cv);
        pthread_mutex_unlock(&q_mtx);
    }
}

int main(int argc, char **argv) {
    int n_threads = 8;
    int argi = 1;
    if (argi + 1 < argc && strcmp(argv[argi], "-j") == 0) {
        n_threads = atoi(argv[argi + 1]);
        if (n_threads < 1) n_threads = 1;
        if (n_threads > 16) n_threads = 16;
        argi += 2;
    }
    if (argi + 1 >= argc) {
        fprintf(stderr, "Usage: %s [-j N] <mod_dir> <modules.load>\n", argv[0]);
        return 2;
    }
    const char *mod_dir = argv[argi];
    const char *list_file = argv[argi + 1];

    double t_start = now_ms();
    FILE *fp = fopen(list_file, "r");
    if (!fp) {
        perror("fopen modules.load");
        return 1;
    }

    char line[256];
    while (fgets(line, sizeof(line), fp) && n_mods < MAX_MODS) {
        size_t len = strlen(line);
        while (len > 0 && (line[len - 1] == '\n' || line[len - 1] == '\r' || line[len - 1] == ' '))
            line[--len] = '\0';
        if (len == 0 || line[0] == '#') continue;

        struct mod_info *m = &mods[n_mods];
        memset(m, 0, sizeof(*m));
        snprintf(m->file, sizeof(m->file), "%s", line);
        snprintf(m->path, sizeof(m->path), "%s/%s", mod_dir, line);
        char stem[128];
        snprintf(stem, sizeof(stem), "%s", line);
        char *dot = strstr(stem, ".ko");
        if (dot) *dot = '\0';
        norm_name(stem, m->name, sizeof(m->name));
        m->idx = n_mods;
        m->head_edge = -1;
        n_mods++;
    }
    fclose(fp);

    pthread_t threads[16];
    for (int i = 0; i < n_threads; i++) {
        pthread_create(&threads[i], NULL, parse_worker, NULL);
    }
    for (int i = 0; i < n_threads; i++) {
        pthread_join(threads[i], NULL);
    }
    build_edges();

    double t_dag = now_ms();

    remaining = n_mods;
    for (int i = 0; i < n_mods; i++) {
        if (mods[i].in_deg == 0) {
            ready_q[q_tail++] = i;
        }
    }

    for (int i = 0; i < n_threads; i++) {
        pthread_create(&threads[i], NULL, worker_fn, NULL);
    }
    for (int i = 0; i < n_threads; i++) {
        pthread_join(threads[i], NULL);
    }

    /* Retry only transient/missing-dep failures (ENOENT/EAGAIN) or unvisited cycle nodes */
    int retries = 0;
    for (int pass = 0; pass < 2; pass++) {
        int any = 0;
        for (int i = 0; i < n_mods; i++) {
            if (!mods[i].loaded && (!mods[i].failed || mods[i].err == ENOENT || mods[i].err == EAGAIN)) {
                mods[i].failed = 0;
                load_one(i);
                retries++;
                if (mods[i].loaded) any = 1;
            }
        }
        if (!any) break;
    }

    double t_end = now_ms();
    int ok = 0, fail = 0;
    for (int i = 0; i < n_mods; i++) {
        if (mods[i].loaded) ok++;
        else {
            fail++;
            printf("Failed %s (errno=%d: %s)\n", mods[i].file, mods[i].err, strerror(mods[i].err));
        }
        if (mods[i].elapsed_ms >= 30.0) {
            printf("Slow %.1fms %s\n", mods[i].elapsed_ms, mods[i].file);
        }
    }
    printf("fastmod: total=%d ok=%d fail=%d retries=%d edges=%d dag=%.1fms load=%.1fms all=%.1fms (j=%d)\n",
           n_mods, ok, fail, retries, n_edges, t_dag - t_start, t_end - t_dag, t_end - t_start, n_threads);
    return fail ? 1 : 0;
}
