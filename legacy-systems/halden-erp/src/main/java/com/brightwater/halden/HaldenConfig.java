package com.brightwater.halden;

import java.nio.file.Path;
import java.nio.file.Paths;

final class HaldenConfig {
    final int port;
    final Path dbPath;
    final String login;
    final String secret;
    final int ticketTtl;
    final int rateLimit;
    final int rateWindow;
    final boolean resetOnStart;
    final boolean simControl;

    HaldenConfig(int port, Path dbPath, String login, String secret, int ticketTtl,
                 int rateLimit, int rateWindow, boolean resetOnStart, boolean simControl) {
        if (port < 0 || port > 65535 || ticketTtl < 0 || rateLimit < 0 || rateWindow < 1) {
            throw new IllegalArgumentException("Invalid HALDEN configuration");
        }
        this.port = port;
        this.dbPath = dbPath;
        this.login = login;
        this.secret = secret;
        this.ticketTtl = ticketTtl;
        this.rateLimit = rateLimit;
        this.rateWindow = rateWindow;
        this.resetOnStart = resetOnStart;
        this.simControl = simControl;
    }

    static HaldenConfig fromEnv() {
        return new HaldenConfig(
                Integer.parseInt(env("HALDEN_PORT", "8090")),
                Paths.get(env("HALDEN_DB_PATH", "/var/lib/halden/halden.db")),
                env("HALDEN_LOGIN", "svc_bridge"),
                env("HALDEN_SECRET", "halden-demo-secret"),
                Integer.parseInt(env("HALDEN_TICKET_TTL_SEC", "900")),
                Integer.parseInt(env("HALDEN_RATE_LIMIT", "30")),
                Integer.parseInt(env("HALDEN_RATE_WINDOW_SEC", "10")),
                Boolean.parseBoolean(env("HALDEN_RESET_ON_START", "true")),
                Boolean.parseBoolean(env("HALDEN_SIM_CONTROL", "true")));
    }

    private static String env(String key, String defaultValue) {
        String value = System.getenv(key);
        return value == null ? defaultValue : value;
    }
}
