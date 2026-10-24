/*
 * Thermostat HTTP API Daemon
 *
 * A standalone HTTP server that exposes thermostat state and control via REST API.
 * Reads state from log files and controls temperature via IPC messages.
 *
 * Endpoints:
 *   GET  /api/state     - Returns current thermostat state as JSON
 *   POST /api/control   - Set temperature (JSON body: {"mode": 2, "temperature": 72})
 *   GET  /health        - Health check endpoint
 *
 * Usage:
 *   ./thermostat_api              # Run on default port 8080
 *   ./thermostat_api -p 8888      # Run on custom port
 *   ./thermostat_api -d           # Daemonize (run in background)
 *
 * Cross-compile:
 *   zig cc -target arm-linux-musleabihf -static -o thermostat_api thermostat_api.c
 *
 * Derived from jeffmcfadden/midea-thermostat (Unlicense), which discovered the
 * open ADB access and the dev_app IPC format.
 *
 * hvac fork (2026-10-19): communicating-mode (HBS/AHU3) state parsing,
 * cached state, mode/fan control using frames captured from the ACiQ app,
 * independent temperature/mode/fan control, client IP allowlist (-a),
 * full-body request reads.
 *
 * SAFETY: State reading is independent and read-only. Temperature control
 * uses the same IPC format as the NetHome Plus app.
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <signal.h>
#include <errno.h>
#include <time.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <fcntl.h>
#include <dirent.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <sys/ipc.h>
#include <sys/msg.h>
#include <netinet/in.h>
#include <arpa/inet.h>

#define DEFAULT_PORT 8080
#define MAX_REQUEST 4096
#define MAX_RESPONSE 8192
#define MAX_LINE 2048
#ifndef LOG_FILE
#define LOG_FILE "/tmp/.lastlog"
#endif
#ifndef LOG_FILE_OLD
#define LOG_FILE_OLD "/tmp/.lastlog.old"
#endif
#ifndef UPTIME_FILE
#define UPTIME_FILE "/proc/uptime"
#endif
#ifndef OS_RELEASE_FILE
#define OS_RELEASE_FILE "/etc/openwrt_release"
#endif
#define MAX_WAITERS 4          /* concurrent long-poll children */
#define MAX_WAIT_SECONDS 30

/* IPC constants for temperature control */
#ifndef DEV_MSG_PATH
#define DEV_MSG_PATH "/tmp/dev_msg"
#endif
#define FTOK_PROJ_ID 1
#define EVENT_MSMART_FRAME 196608  /* 0x30000 */
#define MSMART_FRAME_SIZE 29
#define MSMART_MODE_FRAME_SIZE 47
#define MSG_BUF_SIZE 1056

/* Temperature limits */
#define MIN_TEMP 60
#define MAX_TEMP 86

/* Mode values */

/* Global flag for graceful shutdown */
static volatile int running = 1;

/* System V message structure for IPC */
struct sysv_msg {
    long mtype;
    char mtext[MSG_BUF_SIZE];
};

/* Thermostat state structure */
struct thermostat_state {
    float current_temp;
    float humidity;
    float setpoint;      /* Active setpoint regardless of mode */
    float heat_limit;    /* Lower limit (Tlow, for auto mode) */
    float cool_limit;    /* Upper limit (Thigh, for auto mode) */
    int mode;
    int fan_mode;
    int compressor_state;
    int wifi_connected;
    int wifi_rssi;
    char last_update[32];
    /* Communicating (HBS) fields from dev_app RAC_DEV_AHU3_STATE lines */
    int have_dev_state;
    int dev_power;
    int dev_mode;        /* raw dev_app mode enum (meaning not yet mapped) */
    int dev_auto_state;
    float dev_temp_set;
    float zone_lower;
    float zone_upper;
    int elec_heat;
    int elec_heat_only;
    int dev_wind;
    int state_from_memory;  /* 1 = dev fields filled from dev_app memory (cold start) */
    int dev_temp_unit;      /* thermostat display/setpoint unit: 1 = F, 0 = C (setpoints use it) */
    /* meiju UI values */
    float ui_heat_sp;
    float ui_cool_sp;
    float raw_temp;
};

/* Last known state; survives log rotation (setpoint unit defaults to F until seen) */
static struct thermostat_state cached_state = {.dev_temp_unit = 1};
/* Control frames were verified on this firmware only; other builds -> read-only (override: -F) */
#define VERIFIED_FIRMWARE "tina.tangly.20260314.152301"
static char firmware_id[64] = "unknown";
static int control_enabled = 0;
#define MAX_ALLOWED 8
static struct in_addr allowed_ips[MAX_ALLOWED];
static int n_allowed = 0;

/* Signal handler for graceful shutdown */
void handle_signal(int sig) {
    if (sig == SIGINT || sig == SIGTERM) {
        running = 0;
        _exit(0);  /* accept() auto-restarts, so exit directly */
    }
}

/* Parse one log line into the state (same logic as upstream thermostat_state.c) */
static void parse_line(const char *line, struct thermostat_state *state) {
    {
        /* Extract timestamp */
        if (strlen(line) > 23 && line[0] == '2' && line[4] == '-' && line[10] == ' ') {
            strncpy(state->last_update, line, 23);
            state->last_update[23] = '\0';
        }

        /* Only dev_app and meiju lines carry state. Skip everything else cheaply: with the
           cloud unreachable, msmart alone logs ~150 lines/s. */
        if (!strstr(line, " dev_app: ") && !strstr(line, " meiju: "))
            return;

        /* Parse sensor data: sensor->f_temp = X  sensor->humi_val = Y */
        if (strstr(line, "sensor->f_temp")) {
            float temp;
            char *p = strstr(line, "f_temp");
            /* Raw sensor (reads ~4F high from board self-heating): keep only as
               fallback; the compensated t_after_comp line is authoritative. */
            if (p && sscanf(p, "f_temp = %f", &temp) == 1) {
                state->raw_temp = temp;
                if (state->current_temp == 0) state->current_temp = temp;
            }
        }

        /* Parse compensated temp: t_after_comp = X h_after_comp = Y */
        if (strstr(line, "t_after_comp")) {
            float temp;
            char *p = strstr(line, "t_after_comp");
            if (p && sscanf(p, "t_after_comp = %f", &temp) == 1) {
                state->current_temp = temp;
            }
            p = strstr(line, "h_after_comp");
            if (p) {
                float humi;
                if (sscanf(p, "h_after_comp = %f", &humi) == 1) {
                    state->humidity = humi;
                }
            }
        }

        /* Parse 24V state: Tt:X Thigh:X Tlow:X Ts:X */
        if (strstr(line, "Tt:") && strstr(line, "Thigh:")) {
            char *p;
            float val;
            p = strstr(line, "Thigh:");
            if (p && sscanf(p, "Thigh:%f", &val) == 1) {
                state->cool_limit = val;
            }
            p = strstr(line, "Tlow:");
            if (p && sscanf(p, "Tlow:%f", &val) == 1) {
                state->heat_limit = val;
            }
            p = strstr(line, "Ts:");
            if (p && sscanf(p, "Ts:%f", &val) == 1) {
                state->setpoint = val;
            }
        }

        /* Parse UI refresh: mode=X, fan_state=X, compressor_state=X */
        if (strstr(line, "page_ui_refresh_background")) {
            int val;
            char *p = strstr(line, "mode=");
            if (p && sscanf(p, "mode=%d", &val) == 1) {
                state->mode = val;
            }
            p = strstr(line, "fan_state=");
            if (p && sscanf(p, "fan_state=%d", &val) == 1) {
                state->fan_mode = val;
            }
            p = strstr(line, "compressor_state=");
            if (p && sscanf(p, "compressor_state=%d", &val) == 1) {
                state->compressor_state = val;
            }
            /* Note: Don't use coolTemperValue - use Ts from 24V state instead */
        }

        /* Communicating mode: RAC_DEV_AHU3_STATE, power: 1, mode: 1, op_mode =255 auto_state =4, temp_unit =1, temp_set: 67.000000 */
        if (strstr(line, "RAC_DEV_AHU3_STATE, power:")) {
            int pw, md, op, as, tu; float ts;
            char *p = strstr(line, "power:");
            if (p && sscanf(p, "power: %d, mode: %d, op_mode =%d auto_state =%d, temp_unit =%d, temp_set: %f",
                            &pw, &md, &op, &as, &tu, &ts) == 6) {
                state->have_dev_state = 1;
                state->dev_power = pw;
                state->dev_mode = md;
                state->dev_auto_state = as;
                state->dev_temp_set = ts;
                state->setpoint = ts;
                state->dev_temp_unit = tu ? 1 : 0;
                p = strstr(line, "wind_speed:");
                if (p) sscanf(p, "wind_speed: %d", &state->dev_wind);
            }
        }
        if (strstr(line, "RAC_DEV_AHU3_STATE, fan_state:")) {
            int val; char *p;
            p = strstr(line, "elec_heat:");
            if (p && sscanf(p, "elec_heat: %d", &val) == 1) state->elec_heat = val;
            p = strstr(line, "elec_heat_only:");
            if (p && sscanf(p, "elec_heat_only:%d", &val) == 1) state->elec_heat_only = val;
            p = strstr(line, "compressor_state =");
            if (p && sscanf(p, "compressor_state =%d", &val) == 1) state->compressor_state = val;
        }
        if (strstr(line, "RAC_DEV_AHU3_STATE, temp_zone_control:")) {
            float val; char *p;
            p = strstr(line, "temp_zone_lower_limit =");
            if (p && sscanf(p, "temp_zone_lower_limit =%f", &val) == 1) state->zone_lower = val;
            p = strstr(line, "temp_zone_upper_limit=");
            if (p && sscanf(p, "temp_zone_upper_limit=%f", &val) == 1) state->zone_upper = val;
        }
        if (strstr(line, "page_ui_refresh_background") && strstr(line, "heatTemperValue=")) {
            float val; char *p;
            p = strstr(line, "heatTemperValue=");
            if (p && sscanf(p, "heatTemperValue=%f", &val) == 1) state->ui_heat_sp = val;
            p = strstr(line, "coolTemperValue=");
            if (p && sscanf(p, "coolTemperValue=%f", &val) == 1) state->ui_cool_sp = val;
        }

        /* Parse wifi state */
        if (strstr(line, "wifi_connected:")) {
            int val;
            char *p = strstr(line, "wifi_connected:");
            if (p && sscanf(p, "wifi_connected:%d", &val) == 1) {
                state->wifi_connected = val;
            }
            p = strstr(line, "wifi_rssi:");
            if (p && sscanf(p, "wifi_rssi:%d", &val) == 1) {
                state->wifi_rssi = val;
            }
        }
    }
}

/*
 * Incremental log reading. The thermostat's logread appends to /tmp/.lastlog and
 * rotates it by renaming to .lastlog.old. Re-reading ~1 MB per request costs
 * ~0.5 s CPU on this SoC, so after the first full read only new complete lines
 * are parsed. Rotation is detected by inode change; the unread tail of the
 * rotated file is picked up from .lastlog.old.
 */
struct log_cursor { ino_t ino; long off; int primed; };
static struct log_cursor cur_new;

/* Parse complete lines of `filename` from *off; advance *off past them. */
static void parse_from(const char *filename, long *off, struct thermostat_state *state) {
    FILE *f = fopen(filename, "r");
    if (!f) return;
    if (fseek(f, *off, SEEK_SET) != 0) { fclose(f); return; }
    char line[MAX_LINE];
    while (fgets(line, sizeof(line), f)) {
        size_t len = strlen(line);
        if (len == 0 || line[len - 1] != '\n') {
            if (len == sizeof(line) - 1) {   /* over-long line: skip it whole */
                *off = ftell(f);
                continue;
            }
            break;                           /* partial line still being written */
        }
        parse_line(line, state);
        *off = ftell(f);
    }
    fclose(f);
}

static void refresh_from_log(struct thermostat_state *state) {
    struct stat st_new, st_old;
    int have_new = stat(LOG_FILE, &st_new) == 0;
    if (!cur_new.primed) {
        long off_old = 0;
        parse_from(LOG_FILE_OLD, &off_old, state);
        cur_new.off = 0;
        if (have_new) {
            cur_new.ino = st_new.st_ino;
            parse_from(LOG_FILE, &cur_new.off, state);
        }
        cur_new.primed = 1;
        return;
    }
    if (!have_new) return;
    if (st_new.st_ino == cur_new.ino && st_new.st_size == cur_new.off)
        return;                              /* nothing new: one stat() and done */
    if (st_new.st_ino != cur_new.ino) {
        /* rotated: finish the previous file (now .old) from where we stopped */
        if (stat(LOG_FILE_OLD, &st_old) == 0 && st_old.st_ino == cur_new.ino)
            parse_from(LOG_FILE_OLD, &cur_new.off, state);
        cur_new.ino = st_new.st_ino;
        cur_new.off = 0;
    } else if (st_new.st_size < cur_new.off) {
        cur_new.off = 0;                     /* truncated in place */
    }
    parse_from(LOG_FILE, &cur_new.off, state);
}

struct equipment;
static void read_equipment(struct equipment *eq);
static void fill_from_memory(struct thermostat_state *state);

/* Get current thermostat state */
void get_state(struct thermostat_state *state) {
    *state = cached_state;
    refresh_from_log(state);
    /* No dev_app setpoint seen yet: fall back to the UI's heat setpoint */
    if (!state->have_dev_state)
        fill_from_memory(state);  /* cold start: no AHU3 line in the log window yet */
    if (!state->have_dev_state && state->setpoint == 0 && state->ui_heat_sp > 0)
        state->setpoint = state->ui_heat_sp;
    cached_state = *state;
}

static void read_firmware_id(void) {
    FILE *f = fopen(OS_RELEASE_FILE, "r");
    if (!f) return;
    char line[128];
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, "DISTRIB_ID='", 12) == 0) {
            char *e = strchr(line + 12, '\'');
            if (e) *e = '\0';
            snprintf(firmware_id, sizeof(firmware_id), "%s", line + 12);
        }
    }
    fclose(f);
}

/*
 * Equipment data read (read-only) from dev_app memory: g_self_state (dev_state_t,
 * 312 bytes) at a fixed address in this non-PIE build. Offsets from dev_app DWARF;
 * meanings verified 2026-10-19 with supervised furnace and heat-pump runs.
 * Only used on VERIFIED_FIRMWARE, and only if the layout canary matches.
 */
#define G_SELF_STATE_ADDR 0x5fa3c
#define G_SELF_STATE_SIZE 312
struct equipment {
    int ok;
    int t4, t1, t2a, t3;           /* raw: degC = (raw - 40) / 2; 0/255 = invalid */
    int compressor_hz, furnace_on, blower_on, blower_speed;
    int fault[4], fault_state, protect_state;
    /* current settings (state_setting), used when no AHU3 log line was seen yet */
    int set_power, set_mode, set_wind, set_temp, set_aux, set_aux_alone, set_unit;
};

static pid_t find_pid(const char *name) {
    DIR *d = opendir("/proc");
    if (!d) return -1;
    struct dirent *e;
    pid_t found = -1;
    while ((e = readdir(d)) && found < 0) {
        if (e->d_name[0] < '0' || e->d_name[0] > '9') continue;
        char path[64], comm[32] = {0};
        snprintf(path, sizeof(path), "/proc/%s/comm", e->d_name);
        FILE *f = fopen(path, "r");
        if (!f) continue;
        if (fgets(comm, sizeof(comm), f)) {
            comm[strcspn(comm, "\n")] = '\0';
            if (strcmp(comm, name) == 0) found = (pid_t)atoi(e->d_name);
        }
        fclose(f);
    }
    closedir(d);
    return found;
}

/*
 * dev_app's /proc/<pid>/mem is kept open between reads: a steady-state read is a single
 * pread(). If dev_app restarts the pread fails, and we look it up again (scanning /proc
 * at most every 10 s), so long-poll children stay at a few syscalls per second.
 */
static int dev_mem_fd = -1;
static time_t dev_mem_next_scan = 0;

static int dev_mem_open(void) {
    if (dev_mem_fd >= 0) return dev_mem_fd;
    time_t now = time(NULL);
    if (now < dev_mem_next_scan) return -1;
    dev_mem_next_scan = now + 10;
    pid_t pid = find_pid("dev_app");
    if (pid <= 0) return -1;
    char path[64];
    snprintf(path, sizeof(path), "/proc/%d/mem", (int)pid);
    dev_mem_fd = open(path, O_RDONLY);
    if (dev_mem_fd >= 0) dev_mem_next_scan = 0;
    return dev_mem_fd;
}

static void read_equipment(struct equipment *eq) {
    memset(eq, 0, sizeof(*eq));
    if (strcmp(firmware_id, VERIFIED_FIRMWARE) != 0) return;
    unsigned char b[G_SELF_STATE_SIZE];
    ssize_t n = -1;
    for (int attempt = 0; attempt < 2 && n != (ssize_t)sizeof(b); attempt++) {
        int fd = dev_mem_open();
        if (fd < 0) return;
        n = pread(fd, b, sizeof(b), G_SELF_STATE_ADDR);
        if (n != (ssize_t)sizeof(b)) {          /* dev_app restarted: reopen once */
            close(dev_mem_fd);
            dev_mem_fd = -1;
            dev_mem_next_scan = 0;
        }
    }
    if (n != (ssize_t)sizeof(b)) return;
    /* Layout canary: state_setting.TsMinSet / TsMaxSet = device limits 60 / 86 F */
    if (b[100] != 60 || b[102] != 86) return;
    eq->ok = 1;
    eq->t4 = b[104];
    eq->t1 = b[134];
    eq->t2a = b[135];
    eq->t3 = b[137];
    eq->fault[0] = b[116]; eq->fault[1] = b[118]; eq->fault[2] = b[120]; eq->fault[3] = b[122];
    eq->fault_state = b[160];
    eq->protect_state = b[162];
    eq->blower_on = b[297];
    eq->blower_speed = b[298] | (b[299] << 8);
    eq->compressor_hz = b[304];
    eq->furnace_on = b[307];
    eq->set_aux_alone = b[87];
    eq->set_power = b[93];
    eq->set_mode = b[94];
    eq->set_wind = b[97];
    eq->set_temp = b[98];
    eq->set_unit = b[99];            /* state_setting.TsUnit: 1 = F, 0 = C */
    eq->set_aux = b[107];
}

static void fill_from_memory(struct thermostat_state *state) {
    struct equipment eq;
    read_equipment(&eq);
    /* sanity: mode in the known set, setpoint inside device limits */
    if (!eq.ok) return;
    if (eq.set_unit ? (eq.set_temp < 60 || eq.set_temp > 86) : (eq.set_temp < 16 || eq.set_temp > 30)) return;
    if (eq.set_mode != 1 && eq.set_mode != 2 && eq.set_mode != 4 && eq.set_mode != 5) return;
    state->have_dev_state = 1;
    state->state_from_memory = 1;
    state->dev_power = eq.set_power ? 1 : 0;
    state->dev_mode = eq.set_mode;
    state->dev_temp_set = (float)eq.set_temp;
    state->setpoint = (float)eq.set_temp;
    state->dev_wind = eq.set_wind <= 3 ? eq.set_wind : -1;
    state->elec_heat = eq.set_aux ? 1 : 0;
    state->elec_heat_only = eq.set_aux_alone ? 1 : 0;
    state->dev_temp_unit = eq.set_unit ? 1 : 0;
}

/* raw 0xC0 temperature -> "%.1f" degF or "null" */
static const char *raw_temp_f(int raw, char *buf, size_t sz) {
    if (raw <= 0 || raw >= 255) { snprintf(buf, sz, "null"); return buf; }
    snprintf(buf, sz, "%.1f", ((raw - 40) / 2.0) * 9.0 / 5.0 + 32.0);
    return buf;
}

static int build_equipment_json(const struct equipment *eqp, char *buf, size_t sz) {
    const struct equipment eq = *eqp;
    if (!eq.ok) return snprintf(buf, sz, "{\"available\": false}");
    char a[16], b2[16], c[16], d[16];
    return snprintf(buf, sz,
        "{\"available\": true, \"outdoor_temp\": %s, \"indoor_unit_t1\": %s, "
        "\"indoor_coil_temp\": %s, \"outdoor_coil_temp\": %s, \"compressor_hz\": %d, "
        "\"furnace_on\": %s, \"blower_on\": %s, \"blower_speed\": %d, "
        "\"fault_codes\": [%d, %d, %d, %d], \"fault_state\": %d, \"protect_state\": %d}",
        raw_temp_f(eq.t4, a, sizeof(a)), raw_temp_f(eq.t1, b2, sizeof(b2)),
        raw_temp_f(eq.t2a, c, sizeof(c)), raw_temp_f(eq.t3, d, sizeof(d)),
        eq.compressor_hz, eq.furnace_on ? "true" : "false", eq.blower_on ? "true" : "false",
        eq.blower_speed, eq.fault[0], eq.fault[1], eq.fault[2], eq.fault[3],
        eq.fault_state, eq.protect_state);
}

/*
 * HVAC mode from dev_app AHU3 state (mapped 2026-10-19 from screen + app captures):
 *   power 0 -> off; mode 1 auto, 2 cool, 4 heat (elec_heat_only=1 -> emergency heat), 5 fan only
 */
const char *hvac_mode_string(const struct thermostat_state *st) {
    if (!st->have_dev_state) return "unknown";
    if (!st->dev_power) return "off";
    switch (st->dev_mode) {
        case 1: return "auto";
        case 2: return "cool";
        case 4: return st->elec_heat_only ? "emergency_heat" : "heat";
        case 5: return "fan_only";
        default: return "unknown";
    }
}

/* wind_speed 0..3 <- app fan values 30/60/90/102 */
const char *fan_string(int wind) {
    switch (wind) {
        case 0: return "low";
        case 1: return "medium";
        case 2: return "high";
        case 3: return "auto";
        default: return "unknown";
    }
}

/* Seconds since boot from /proc/uptime (-1 if unavailable) */
static long read_uptime(void) {
    FILE *f = fopen(UPTIME_FILE, "r");
    if (!f) return -1;
    double up = -1;
    if (fscanf(f, "%lf", &up) != 1) up = -1;
    fclose(f);
    return (long)up;
}
static long daemon_start_uptime = -1;

/*
 * Fingerprint of the fields a client acts on (mode/setpoints/fan/aux/auto state and
 * equipment run states). Room temperature/humidity are deliberately excluded: they
 * change constantly and would defeat long-polling.
 */
static void state_id(const struct thermostat_state *st, const struct equipment *eq, char out[9]) {
    char sig[256];
    snprintf(sig, sizeof(sig), "%s|%c|%.1f|%d|%d|%d|%d|%d|%.0f|%.0f|%d|%d|%d|%d|%d|%d|%d|%d|%d|%d|%d|%d",
             hvac_mode_string(st), st->dev_temp_unit ? 'F' : 'C', st->setpoint, st->dev_power, st->have_dev_state ? st->dev_wind : -1,
             st->elec_heat, st->elec_heat_only, st->dev_auto_state, st->ui_heat_sp, st->ui_cool_sp,
             eq->ok, eq->ok ? (eq->compressor_hz + 2) / 5 : -1, eq->furnace_on, eq->blower_on,
             eq->blower_speed, eq->fault[0], eq->fault[1], eq->fault[2], eq->fault[3],
             eq->fault_state, eq->protect_state, control_enabled);
    uint32_t h = 2166136261u;                       /* FNV-1a */
    for (const char *c = sig; *c; c++) { h ^= (unsigned char)*c; h *= 16777619u; }
    snprintf(out, 9, "%08x", (unsigned)h);
}

/* Current state + its id (one equipment read) */
static void snapshot(struct thermostat_state *st, struct equipment *eq, char id[9]) {
    get_state(st);
    read_equipment(eq);
    state_id(st, eq, id);
}

/* Build JSON response for state */
static int build_state_json_from(const struct thermostat_state *stp, const struct equipment *eq,
                                 const char *sid, char *buf, size_t bufsize) {
    const struct thermostat_state state = *stp;
    char equip_json[512];
    build_equipment_json(eq, equip_json, sizeof(equip_json));
    long up = read_uptime();

    return snprintf(buf, bufsize,
        "{\n"
        "  \"current_temp\": %.1f,\n"
        "  \"humidity\": %.1f,\n"
        "  \"mode\": \"%s\",\n"
        "  \"fan\": \"%s\",\n"
        "  \"ui_mode_raw\": %d,\n"
        "  \"setpoint\": %.0f,\n"
        "  \"temp_unit\": \"%s\",\n"
        "  \"heat_limit\": %.0f,\n"
        "  \"cool_limit\": %.0f,\n"
        "  \"fan_mode\": %d,\n"
        "  \"compressor_running\": %s,\n"
        "  \"wifi_connected\": %s,\n"
        "  \"wifi_rssi\": %d,\n"
        "  \"last_update\": \"%s\",\n"
        "  \"firmware\": \"%s\",\n"
        "  \"control_enabled\": %s,\n"
        "  \"state_id\": \"%s\",\n"
        "  \"uptime_s\": %ld,\n"
        "  \"daemon_uptime_s\": %ld,\n"
        "  \"dev\": {\"seen\": %s, \"power\": %d, \"mode_raw\": %d, \"auto_state\": %d, "
        "\"source\": \"%s\", \"temp_set\": %.1f, \"zone_lower\": %.1f, \"zone_upper\": %.1f, \"elec_heat\": %d, \"elec_heat_only\": %d, \"wind_speed\": %d},\n"
        "  \"ui\": {\"heat_setpoint\": %.0f, \"cool_setpoint\": %.0f},\n"
        "  \"equipment\": %s\n"
        "}\n",
        state.current_temp,
        state.humidity,
        hvac_mode_string(&state),
        state.have_dev_state ? fan_string(state.dev_wind) : "unknown",
        state.mode,
        state.setpoint,
        state.dev_temp_unit ? "F" : "C",
        state.heat_limit,
        state.cool_limit,
        state.fan_mode,
        state.compressor_state ? "true" : "false",
        state.wifi_connected ? "true" : "false",
        state.wifi_rssi,
        state.last_update,
        firmware_id,
        control_enabled ? "true" : "false",
        sid, up, (up >= 0 && daemon_start_uptime >= 0) ? up - daemon_start_uptime : -1L,
        state.have_dev_state ? "true" : "false", state.dev_power, state.dev_mode,
        state.dev_auto_state, state.state_from_memory ? "memory" : "log", state.dev_temp_set, state.zone_lower, state.zone_upper,
        state.elec_heat, state.elec_heat_only, state.dev_wind,
        state.ui_heat_sp, state.ui_cool_sp,
        equip_json
    );
}

int build_state_json(char *buf, size_t bufsize) {
    struct thermostat_state st;
    struct equipment eq;
    char id[9];
    snapshot(&st, &eq, id);
    return build_state_json_from(&st, &eq, id, buf, bufsize);
}

/*
 * Set thermostat temperature via IPC
 *
 * Uses msmart frame format (same as NetHome Plus app).
 * Note: This only sets the setpoint temperature. It does NOT change
 * the HVAC mode (heat/cool). Mode must be changed via the thermostat UI.
 *
 * Returns 0 on success, -1 on error.
 */
/*
 * Celsius-mode setpoint (verified 2026-10-24 on a TL04-1 in C): a frame with only key 0x03
 * (no 0x41 unit key) makes dev_app use its current unit -> "TempUnit:0,temperature:21",
 * setTemp(.TsUnit=0 .Ts=21). Sending the 0x41 key instead would call setTempUnit() and
 * switch the display unit.
 */
int send_msmart_frame(const unsigned char *frame, size_t len, char *error_buf, size_t error_size);
#define MIN_TEMP_C 16
#define MAX_TEMP_C 30
int set_temperature_c(int temperature, char *error_buf, size_t error_size) {
    if (temperature < MIN_TEMP_C || temperature > MAX_TEMP_C) {
        snprintf(error_buf, error_size, "Temperature must be between %d and %d C", MIN_TEMP_C, MAX_TEMP_C);
        return -1;
    }
    unsigned char frame[23] = {
        0xaa, 0x16, 0x44, 0x00, 0x00, 0x00, 0x00, 0x00, 0x02, 0x02, 0xd0, 0x01,
        0x03, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00
    };
    frame[17] = (unsigned char)(temperature * 2 + 50);
    return send_msmart_frame(frame, sizeof(frame), error_buf, error_size);
}

int set_temperature(int temperature, char *error_buf, size_t error_size) {
    /* Validate temperature */
    if (temperature < MIN_TEMP || temperature > MAX_TEMP) {
        snprintf(error_buf, error_size, "Temperature must be between %d and %d",
                 MIN_TEMP, MAX_TEMP);
        return -1;
    }

    /* Build msmart frame - byte 17 must be 0x02 (command type, not HVAC mode) */
    unsigned char frame[MSMART_FRAME_SIZE] = {
        0xaa, 0x1c, 0x44, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x02, 0x02, 0xd0, 0x02, 0x41, 0x00, 0x00, 0x00,
        0x01, 0x02, 0x03, 0x00, 0x00, 0x00, 0x01, 0xc6,
        0x00, 0x00, 0xc9, 0x5d, 0x96
    };

    /* Encode temperature at byte 23 */
    frame[23] = (unsigned char)(temperature * 2 + 50);

    /* Build IPC message */
    struct sysv_msg msg;
    memset(&msg, 0, sizeof(msg));
    msg.mtype = 1;

    /* Set event type at offset 4 */
    *(uint32_t *)&msg.mtext[4] = EVENT_MSMART_FRAME;

    /* Set frame size at offset 8 */
    *(uint32_t *)&msg.mtext[8] = MSMART_FRAME_SIZE;

    /* Copy frame data at offset 12 */
    memcpy(&msg.mtext[12], frame, MSMART_FRAME_SIZE);

    /* Get IPC queue */
    key_t key = ftok(DEV_MSG_PATH, FTOK_PROJ_ID);
    if (key == -1) {
        snprintf(error_buf, error_size, "ftok failed: %s", strerror(errno));
        return -1;
    }

    int msqid = msgget(key, 0666);
    if (msqid == -1) {
        snprintf(error_buf, error_size, "msgget failed: %s", strerror(errno));
        return -1;
    }

    /* Send the message */
    if (msgsnd(msqid, &msg, MSG_BUF_SIZE, IPC_NOWAIT) == -1) {
        snprintf(error_buf, error_size, "msgsnd failed: %s", strerror(errno));
        return -1;
    }

    return 0;
}

/* Send a raw msmart frame to dev_app (event 0x30000 on /tmp/dev_msg) */
int send_msmart_frame(const unsigned char *frame, size_t len, char *error_buf, size_t error_size) {
    struct sysv_msg msg;
    memset(&msg, 0, sizeof(msg));
    msg.mtype = 1;
    *(uint32_t *)&msg.mtext[4] = EVENT_MSMART_FRAME;
    *(uint32_t *)&msg.mtext[8] = (uint32_t)len;
    memcpy(&msg.mtext[12], frame, len);

    key_t key = ftok(DEV_MSG_PATH, FTOK_PROJ_ID);
    if (key == -1) {
        snprintf(error_buf, error_size, "ftok failed: %s", strerror(errno));
        return -1;
    }
    int msqid = msgget(key, 0666);
    if (msqid == -1) {
        snprintf(error_buf, error_size, "msgget failed: %s", strerror(errno));
        return -1;
    }
    if (msgsnd(msqid, &msg, MSG_BUF_SIZE, IPC_NOWAIT) == -1) {
        snprintf(error_buf, error_size, "msgsnd failed: %s", strerror(errno));
        return -1;
    }
    return 0;
}

/*
 * Mode frames captured verbatim from the ACiQ app via cloud (2026-10-19,
 * communicating/HBS system). TLV keys: 0x02 mode, 0x41 unit, 0x67 aux
 * (02 normal, 03 emergency heat), 0x01 power-on (03), 0x1f (0b heat, else 0a).
 */
struct mode_frame { const char *name; unsigned char f[MSMART_MODE_FRAME_SIZE]; };
#define MODE_HDR 0xaa,0x2e,0x44,0x00,0x00,0x00,0x00,0x00,0x02,0x02,0xd0,0x05,0x02,0x00,0x00,0x00,0x01
static const struct mode_frame mode_frames[] = {
    {"auto",           {MODE_HDR,0x01,0x41,0,0,0,1,0x02,0x67,0,0,0,1,0x02,0x01,0,0,0,1,0x03,0x1f,0,0,0,1,0x0a,0,0,0x0e,0x93,0x33}},
    {"cool",           {MODE_HDR,0x02,0x41,0,0,0,1,0x02,0x67,0,0,0,1,0x02,0x01,0,0,0,1,0x03,0x1f,0,0,0,1,0x0a,0,0,0x0f,0x54,0x70}},
    {"heat",           {MODE_HDR,0x04,0x41,0,0,0,1,0x02,0x67,0,0,0,1,0x02,0x01,0,0,0,1,0x03,0x1f,0,0,0,1,0x0b,0,0,0x5f,0x5a,0x17}},
    {"fan_only",       {MODE_HDR,0x05,0x41,0,0,0,1,0x02,0x67,0,0,0,1,0x02,0x01,0,0,0,1,0x03,0x1f,0,0,0,1,0x0a,0,0,0xf0,0x18,0xc8}},
    {"emergency_heat", {MODE_HDR,0x04,0x41,0,0,0,1,0x02,0x67,0,0,0,1,0x03,0x01,0,0,0,1,0x03,0x1f,0,0,0,1,0x0a,0,0,0xcf,0x9a,0x67}},
};

int set_mode(const char *mode, char *error_buf, size_t error_size) {
    for (size_t i = 0; i < sizeof(mode_frames) / sizeof(mode_frames[0]); i++)
        if (strcmp(mode, mode_frames[i].name) == 0)
            return send_msmart_frame(mode_frames[i].f, MSMART_MODE_FRAME_SIZE, error_buf, error_size);
    if (strcmp(mode, "off") == 0) {
        /* Captured from app: key 0x41=02 (unit), key 0x01=02 (power off; 03 = on) */
        static const unsigned char off_frame[MSMART_FRAME_SIZE] = {
            0xaa, 0x1c, 0x44, 0x00, 0x00, 0x00, 0x00, 0x00,
            0x02, 0x02, 0xd0, 0x02, 0x41, 0x00, 0x00, 0x00,
            0x01, 0x02, 0x01, 0x00, 0x00, 0x00, 0x01, 0x02,
            0x00, 0x00, 0x09, 0x79, 0x00
        };
        return send_msmart_frame(off_frame, MSMART_FRAME_SIZE, error_buf, error_size);
    }
    snprintf(error_buf, error_size, "mode must be off|auto|cool|heat|fan_only|emergency_heat");
    return -1;
}

/* Modes where fan speed control is verified (app/screen allow it) */
#define FAN_ALLOWED_MODES "cool,heat"  /* verified 2026-10-19; auto + fan_only lock fan in UI */
static int fan_allowed_in(const char *mode) {
    char list[] = FAN_ALLOWED_MODES;
    for (char *t = strtok(list, ","); t; t = strtok(NULL, ","))
        if (strcmp(t, mode) == 0) return 1;
    return 0;
}

/* Fan frames captured from the app: key 0x04 = 30 low, 60 medium, 90 high, 102 auto */
#define FAN_FRAME_SIZE 23
struct fan_frame { const char *name; unsigned char f[FAN_FRAME_SIZE]; };
#define FAN_HDR 0xaa,0x16,0x44,0x00,0x00,0x00,0x00,0x00,0x02,0x02,0xd0,0x01,0x04,0x00,0x00,0x00,0x01
static const struct fan_frame fan_frames[] = {
    {"low",    {FAN_HDR,0x1e,0,0,0x19,0x37,0x5e}},
    {"medium", {FAN_HDR,0x3c,0,0,0xb9,0x3d,0x9a}},
    {"high",   {FAN_HDR,0x5a,0,0,0x59,0x22,0xf7}},
    {"auto",   {FAN_HDR,0x66,0,0,0x99,0x2e,0x9f}},
};

int set_fan(const char *fan, char *error_buf, size_t error_size) {
    for (size_t i = 0; i < sizeof(fan_frames) / sizeof(fan_frames[0]); i++)
        if (strcmp(fan, fan_frames[i].name) == 0)
            return send_msmart_frame(fan_frames[i].f, FAN_FRAME_SIZE, error_buf, error_size);
    snprintf(error_buf, error_size, "fan must be low|medium|high|auto");
    return -1;
}

/* Extract a string value for "key": "value" into out */
static int json_str(const char *json, const char *key, char *out, size_t outsz) {
    char pat[32];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *p = strstr(json, pat);
    if (!p) return 0;
    p = strchr(p + strlen(pat), ':');
    if (!p) return 0;
    p++;
    while (*p == ' ' || *p == '\t') p++;
    if (*p == '"') {
        p++;
        size_t n = 0;
        while (*p && *p != '"' && n + 1 < outsz) out[n++] = *p++;
        out[n] = '\0';
        return n > 0;
    }
    /* legacy numeric mode from upstream: 1=heat, 2=cool */
    int v = atoi(p);
    if (strcmp(key, "mode") == 0 && (v == 1 || v == 2)) {
        snprintf(out, outsz, "%s", v == 1 ? "heat" : "cool");
        return 1;
    }
    return 0;
}

/* Parse control body: any of temperature / mode / fan; returns -1 if none */
int parse_control_json(const char *json, char *mode, size_t msz, int *temperature, char *fan, size_t fsz) {
    *temperature = 0;
    mode[0] = fan[0] = '\0';
    const char *p = strstr(json, "\"temperature\"");
    if (p && (p = strchr(p, ':'))) *temperature = atoi(p + 1);
    json_str(json, "mode", mode, msz);
    json_str(json, "fan", fan, fsz);
    return (*temperature > 0 || mode[0] || fan[0]) ? 0 : -1;
}

/* Build HTTP response */
int build_http_response(char *buf, size_t bufsize, int status_code,
                        const char *status_text, const char *content_type,
                        const char *body) {
    return snprintf(buf, bufsize,
        "HTTP/1.1 %d %s\r\n"
        "Content-Type: %s\r\n"
        "Content-Length: %zu\r\n"
        "Access-Control-Allow-Origin: *\r\n"
        "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
        "Access-Control-Allow-Headers: Content-Type\r\n"
        "Connection: close\r\n"
        "\r\n"
        "%s",
        status_code, status_text, content_type, strlen(body), body);
}

/* Handle HTTP request */
static int g_server_fd = -1;
static int active_waiters = 0;

/* Value of `key` in a query string ("a=1&b=2"), copied into out; 1 if present */
static int query_param(const char *query, const char *key, char *out, size_t outsz) {
    size_t klen = strlen(key);
    for (const char *q = query; q && *q; ) {
        if (strncmp(q, key, klen) == 0 && q[klen] == '=') {
            const char *v = q + klen + 1;
            size_t n = strcspn(v, "&");
            if (n >= outsz) n = outsz - 1;
            memcpy(out, v, n);
            out[n] = '\0';
            return 1;
        }
        q = strchr(q, '&');
        if (q) q++;
    }
    return 0;
}

static void send_state(int client_fd, const struct thermostat_state *st,
                       const struct equipment *eq, const char *id) {
    char body[MAX_RESPONSE / 2], response[MAX_RESPONSE];
    build_state_json_from(st, eq, id, body, sizeof(body));
    build_http_response(response, sizeof(response), 200, "OK", "application/json", body);
    send(client_fd, response, strlen(response), 0);
}

/*
 * Long-poll: GET /api/state?wait=<state_id>&timeout=<s>. Answers as soon as the
 * state id differs from `wait` (or immediately if it already does), else at the
 * timeout with the current state. Served by a forked child so the main loop keeps
 * handling control commands; at most MAX_WAITERS at once (beyond that: answer now).
 */
static void handle_wait(int client_fd, const char *wait_id, int timeout_s) {
    struct thermostat_state st;
    struct equipment eq;
    char id[9];
    snapshot(&st, &eq, id);
    if (strcmp(id, wait_id) != 0 || timeout_s <= 0 || active_waiters >= MAX_WAITERS) {
        send_state(client_fd, &st, &eq, id);
        close(client_fd);
        return;
    }
    pid_t pid = fork();
    if (pid < 0) {                       /* no fork: degrade to an immediate answer */
        send_state(client_fd, &st, &eq, id);
        close(client_fd);
        return;
    }
    if (pid > 0) {                       /* parent */
        active_waiters++;
        close(client_fd);
        return;
    }
    /* child */
    if (g_server_fd >= 0) close(g_server_fd);
    signal(SIGTERM, SIG_DFL);
    time_t deadline = time(NULL) + timeout_s;
    while (time(NULL) < deadline) {
        fd_set rfds;
        FD_ZERO(&rfds);
        FD_SET(client_fd, &rfds);
        struct timeval tv = {1, 0};
        if (select(client_fd + 1, &rfds, NULL, NULL, &tv) > 0) {
            char tmp[64];
            if (recv(client_fd, tmp, sizeof(tmp), MSG_DONTWAIT) <= 0) _exit(0);  /* client went away */
        }
        snapshot(&st, &eq, id);
        if (strcmp(id, wait_id) != 0) break;
    }
    send_state(client_fd, &st, &eq, id);
    close(client_fd);
    _exit(0);
}

void handle_request(int client_fd) {
    char request[MAX_REQUEST];
    char response[MAX_RESPONSE];
    char body[MAX_RESPONSE / 2];

    /* Read request */
    struct timeval tv = {5, 0};
    setsockopt(client_fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(client_fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    ssize_t n = 0;
    for (;;) {
        ssize_t r = recv(client_fd, request + n, sizeof(request) - 1 - n, 0);
        if (r <= 0) break;
        n += r;
        request[n] = '\0';
        char *hdr_end = strstr(request, "\r\n\r\n");
        if (!hdr_end) { if ((size_t)n >= sizeof(request) - 1) break; continue; }
        long clen = 0;
        char *cl = strcasestr(request, "\r\nContent-Length:");
        if (cl) clen = strtol(cl + 17, NULL, 10);
        if ((hdr_end + 4 - request) + clen <= n || (size_t)n >= sizeof(request) - 1) break;
    }
    if (n <= 0) {
        close(client_fd);
        return;
    }
    request[n] = '\0';

    /* Parse request line */
    char method[16], path[256];
    if (sscanf(request, "%15s %255s", method, path) != 2) {
        strcpy(body, "{\"error\": \"Bad request\"}");
        build_http_response(response, sizeof(response), 400, "Bad Request",
                           "application/json", body);
        send(client_fd, response, strlen(response), 0);
        close(client_fd);
        return;
    }

    /* Handle OPTIONS (CORS preflight) */
    if (strcmp(method, "OPTIONS") == 0) {
        strcpy(body, "");
        build_http_response(response, sizeof(response), 204, "No Content",
                           "text/plain", body);
        send(client_fd, response, strlen(response), 0);
        close(client_fd);
        return;
    }

    /* Route GET requests */
    if (strcmp(method, "GET") == 0) {
        char *query = strchr(path, '?');
        if (query) *query++ = '\0';
        char wait_id[16];
        if ((strcmp(path, "/api/state") == 0 || strcmp(path, "/api/state/") == 0)
                && query && query_param(query, "wait", wait_id, sizeof(wait_id))) {
            char tbuf[8] = "25";
            query_param(query, "timeout", tbuf, sizeof(tbuf));
            int t = atoi(tbuf);
            if (t > MAX_WAIT_SECONDS) t = MAX_WAIT_SECONDS;
            handle_wait(client_fd, wait_id, t);
            return;
        }
        if (strcmp(path, "/api/state") == 0 || strcmp(path, "/api/state/") == 0) {
            /* Return thermostat state */
            build_state_json(body, sizeof(body));
            build_http_response(response, sizeof(response), 200, "OK",
                               "application/json", body);
        }
        else if (strcmp(path, "/health") == 0 || strcmp(path, "/health/") == 0) {
            /* Health check */
            strcpy(body, "{\"status\": \"ok\", \"service\": \"thermostat-api\"}");
            build_http_response(response, sizeof(response), 200, "OK",
                               "application/json", body);
        }
        else if (strcmp(path, "/") == 0) {
            /* Root - return API info */
            strcpy(body,
                "{\n"
                "  \"service\": \"thermostat-api\",\n"
                "  \"endpoints\": [\n"
                "    {\"method\": \"GET\", \"path\": \"/api/state\", \"description\": \"Get thermostat state\"},\n"
                "    {\"method\": \"POST\", \"path\": \"/api/control\", \"description\": \"Set mode and/or temperature\"},\n"
                "    {\"method\": \"GET\", \"path\": \"/health\", \"description\": \"Health check\"}\n"
                "  ]\n"
                "}\n");
            build_http_response(response, sizeof(response), 200, "OK",
                               "application/json", body);
        }
        else {
            /* 404 Not Found */
            strcpy(body, "{\"error\": \"Not found\"}");
            build_http_response(response, sizeof(response), 404, "Not Found",
                               "application/json", body);
        }
    }
    /* Route POST requests */
    else if (strcmp(method, "POST") == 0) {
        if (strcmp(path, "/api/control") == 0 || strcmp(path, "/api/control/") == 0) {
            /* Find request body (after \r\n\r\n) */
            char *body_start = strstr(request, "\r\n\r\n");
            if (body_start) {
                body_start += 4;

                char mode[24], fan[16];
                int temperature = 0;
                if (parse_control_json(body_start, mode, sizeof(mode), &temperature, fan, sizeof(fan)) == 0) {
                    char error_msg[256] = {0};
                    int success = 1;

                    if (!control_enabled) {
                        snprintf(error_msg, sizeof(error_msg),
                                 "control disabled: firmware %s not verified (expected %s)",
                                 firmware_id, VERIFIED_FIRMWARE);
                        success = 0;
                    }

                    /* Fan speed is locked by the UI/app outside some modes (e.g. auto).
                       Only allow it where verified; mode = requested mode or current one. */
                    /* Validate everything before sending anything (no half-applied commands) */
                    if (success && mode[0]) {
                        static const char *valid[] = {"off","auto","cool","heat","fan_only","emergency_heat"};
                        int ok = 0;
                        for (size_t k = 0; k < sizeof(valid)/sizeof(valid[0]); k++)
                            if (strcmp(mode, valid[k]) == 0) ok = 1;
                        if (!ok) {
                            snprintf(error_msg, sizeof(error_msg),
                                     "mode must be off|auto|cool|heat|fan_only|emergency_heat");
                            success = 0;
                        }
                    }
                    /* Setpoints are in the thermostat's current unit; an explicit "unit" that
                       differs is converted (so a client can always send what it displays). */
                    int therm_c = 0;
                    if (success && temperature > 0) {
                        struct thermostat_state cur;
                        get_state(&cur);
                        therm_c = cur.have_dev_state && cur.dev_temp_unit == 0;
                        char unit[4] = "";
                        json_str(body_start, "unit", unit, sizeof(unit));
                        int req_c = unit[0] ? (unit[0] == 'C' || unit[0] == 'c') : therm_c;
                        if (req_c && !therm_c) temperature = (int)(temperature * 9.0 / 5.0 + 32 + 0.5);
                        else if (!req_c && therm_c) temperature = (int)((temperature - 32) * 5.0 / 9.0 + 0.5);
                        int lo = therm_c ? MIN_TEMP_C : MIN_TEMP, hi = therm_c ? MAX_TEMP_C : MAX_TEMP;
                        if (temperature < lo || temperature > hi) {
                            snprintf(error_msg, sizeof(error_msg), "Temperature must be between %d and %d %s",
                                     lo, hi, therm_c ? "C" : "F");
                            success = 0;
                        }
                    }
                    if (success && fan[0] && strcmp(fan,"low") && strcmp(fan,"medium") &&
                        strcmp(fan,"high") && strcmp(fan,"auto")) {
                        snprintf(error_msg, sizeof(error_msg), "fan must be low|medium|high|auto");
                        success = 0;
                    }

                    if (success && fan[0]) {
                        struct thermostat_state cur;
                        get_state(&cur);
                        const char *target = mode[0] ? mode : hvac_mode_string(&cur);
                        if (!fan_allowed_in(target)) {
                            snprintf(error_msg, sizeof(error_msg),
                                     "fan cannot be changed in mode %s (allowed: %s)", target, FAN_ALLOWED_MODES);
                            success = 0;
                        }
                    }

                    /* Same order as the app: mode, then setpoint, then fan */
                    if (success && mode[0]) {
                        if (set_mode(mode, error_msg, sizeof(error_msg)) != 0) success = 0;
                        else if (temperature > 0 || fan[0]) usleep(500000);
                    }
                    if (success && temperature > 0) {
                        if ((therm_c ? set_temperature_c(temperature, error_msg, sizeof(error_msg))
                                     : set_temperature(temperature, error_msg, sizeof(error_msg))) != 0) success = 0;
                        else if (fan[0]) usleep(500000);
                    }
                    if (success && fan[0]) {
                        if (set_fan(fan, error_msg, sizeof(error_msg)) != 0) success = 0;
                    }

                    if (success) {
                        char t[32] = "null";
                        if (temperature > 0) snprintf(t, sizeof(t), "%d", temperature);
                        snprintf(body, sizeof(body),
                            "{\"success\": true, \"mode\": \"%s\", \"temperature\": %s, \"fan\": \"%s\", "
                            "\"message\": \"Control command sent\"}\n",
                            mode, t, fan);
                        build_http_response(response, sizeof(response), 200, "OK",
                                           "application/json", body);
                    } else {
                        snprintf(body, sizeof(body), "{\"error\": \"%s\"}", error_msg);
                        build_http_response(response, sizeof(response), 400, "Bad Request",
                                           "application/json", body);
                    }
                } else {
                    /* Parse error */
                    strcpy(body, "{\"error\": \"Expected any of: temperature 60-86, mode off|auto|cool|heat|fan_only|emergency_heat, fan low|medium|high|auto\"}");
                    build_http_response(response, sizeof(response), 400, "Bad Request",
                                       "application/json", body);
                }
            } else {
                strcpy(body, "{\"error\": \"Missing request body\"}");
                build_http_response(response, sizeof(response), 400, "Bad Request",
                                   "application/json", body);
            }
        }
        else {
            /* 404 Not Found */
            strcpy(body, "{\"error\": \"Not found\"}");
            build_http_response(response, sizeof(response), 404, "Not Found",
                               "application/json", body);
        }
    }
    else {
        /* Method not allowed */
        strcpy(body, "{\"error\": \"Method not allowed\"}");
        build_http_response(response, sizeof(response), 405, "Method Not Allowed",
                           "application/json", body);
    }

    /* Send response */
    send(client_fd, response, strlen(response), 0);
    close(client_fd);
}

/* Daemonize the process */
void daemonize(void) {
    pid_t pid = fork();
    if (pid < 0) {
        perror("fork");
        exit(1);
    }
    if (pid > 0) {
        /* Parent exits */
        printf("Daemon started with PID %d\n", pid);
        exit(0);
    }

    /* Child continues */
    setsid();

    /* Redirect stdio to /dev/null */
    freopen("/dev/null", "r", stdin);
    freopen("/dev/null", "w", stdout);
    freopen("/dev/null", "w", stderr);
}

void print_usage(const char *prog) {
    printf("Thermostat HTTP API Daemon\n\n");
    printf("Usage: %s [options]\n\n", prog);
    printf("Options:\n");
    printf("  -p PORT    Listen port (default: %d)\n", DEFAULT_PORT);
    printf("  -d         Daemonize (run in background)\n");
    printf("  -a IP      Allow client IP (repeatable; default: allow all)\n");
    printf("  -F         Allow control on unverified firmware\n");
    printf("  -h         Show this help\n");
    printf("\n");
    printf("Endpoints:\n");
    printf("  GET  /api/state    - Returns thermostat state as JSON\n");
    printf("       ?wait=<state_id>&timeout=<s>  long-poll: answer on change (max %d s)\n", MAX_WAIT_SECONDS);
    printf("  POST /api/control  - Set mode and temperature\n");
    printf("       Body: any of {\"temperature\": 60-86, \"mode\": \"off|auto|cool|heat|fan_only|emergency_heat\", \"fan\": \"low|medium|high|auto\"}\n");
    printf("  GET  /health       - Health check\n");
    printf("\n");
    printf("Examples:\n");
    printf("  # Set to cooling at 72\n");
    printf("  curl -X POST http://localhost:%d/api/control \\\n", DEFAULT_PORT);
    printf("       -H 'Content-Type: application/json' \\\n");
    printf("       -d '{\"mode\": \"cool\", \"temperature\": 72}'\n");
    printf("\n");
    printf("  # Set to heating at 68\n");
    printf("  curl -X POST http://localhost:%d/api/control \\\n", DEFAULT_PORT);
    printf("       -H 'Content-Type: application/json' \\\n");
    printf("       -d '{\"mode\": \"heat\", \"temperature\": 68}'\n");
}

int main(int argc, char *argv[]) {
    int port = DEFAULT_PORT;
    int daemon_mode = 0;
    int force_control = 0;

    /* Parse arguments */
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "-p") == 0 && i + 1 < argc) {
            port = atoi(argv[++i]);
        } else if (strcmp(argv[i], "-d") == 0) {
            daemon_mode = 1;
        } else if (strcmp(argv[i], "-F") == 0) {
            force_control = 1;
        } else if (strcmp(argv[i], "-a") == 0 && i + 1 < argc) {
            if (n_allowed < MAX_ALLOWED && inet_aton(argv[i + 1], &allowed_ips[n_allowed]))
                n_allowed++;
            i++;
        } else if (strcmp(argv[i], "-h") == 0 || strcmp(argv[i], "--help") == 0) {
            print_usage(argv[0]);
            return 0;
        }
    }

    read_firmware_id();
    daemon_start_uptime = read_uptime();
    control_enabled = force_control || strcmp(firmware_id, VERIFIED_FIRMWARE) == 0;

    /* Set up signal handlers */
    signal(SIGINT, handle_signal);
    signal(SIGTERM, handle_signal);
    signal(SIGPIPE, SIG_IGN);

    /* Create socket */
    int server_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (server_fd < 0) {
        perror("socket");
        return 1;
    }

    /* Allow address reuse */
    int opt = 1;
    setsockopt(server_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    /* Bind to port */
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = INADDR_ANY;
    addr.sin_port = htons(port);

    if (bind(server_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("bind");
        close(server_fd);
        return 1;
    }

    /* Listen */
    if (listen(server_fd, 10) < 0) {
        perror("listen");
        close(server_fd);
        return 1;
    }

    g_server_fd = server_fd;
    printf("Thermostat API server starting on port %d (firmware %s, control %s)\n",
           port, firmware_id, control_enabled ? "enabled" : "DISABLED");
    printf("Endpoints:\n");
    printf("  GET  http://localhost:%d/api/state\n", port);
    printf("  POST http://localhost:%d/api/control\n", port);
    printf("  GET  http://localhost:%d/health\n", port);
    printf("\nPress Ctrl+C to stop.\n\n");

    /* Daemonize if requested (after printing startup info) */
    if (daemon_mode) {
        daemonize();
    }

    /* Accept loop */
    while (running) {
        struct sockaddr_in client_addr;
        socklen_t client_len = sizeof(client_addr);

        int client_fd = accept(server_fd, (struct sockaddr *)&client_addr, &client_len);
        while (waitpid(-1, NULL, WNOHANG) > 0)   /* reap finished long-poll children */
            if (active_waiters > 0) active_waiters--;
        if (client_fd < 0) {
            if (errno == EINTR) continue;  /* Signal interrupted */
            perror("accept");
            continue;
        }

        if (n_allowed > 0) {
            int ok = 0;
            for (int k = 0; k < n_allowed; k++)
                if (allowed_ips[k].s_addr == client_addr.sin_addr.s_addr) ok = 1;
            if (!ok) { close(client_fd); continue; }
        }

        handle_request(client_fd);
    }

    printf("\nShutting down...\n");
    close(server_fd);
    return 0;
}
