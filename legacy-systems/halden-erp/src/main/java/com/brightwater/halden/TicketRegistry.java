package com.brightwater.halden;

import java.util.Locale;
import java.util.concurrent.TimeUnit;

final class TicketRegistry {
    enum Status { UNKNOWN, EXPIRED, ACTIVE }

    private final HaldenConfig config;
    private long sequence;
    private int ttl;
    private String current;
    private long expiresAt;

    TicketRegistry(HaldenConfig config) {
        this.config = config;
        this.ttl = config.ticketTtl;
    }

    synchronized String grant(String login, String secret) {
        if (!config.login.equals(login) || !config.secret.equals(secret)) {
            return "STATUS=DENIED\r\nREASON=BAD_CREDENTIALS\r\n";
        }
        current = String.format(Locale.ROOT, "%032X", ++sequence);
        expiresAt = System.nanoTime() + TimeUnit.SECONDS.toNanos(ttl);
        return "STATUS=GRANTED\r\nTICKET=" + current + "\r\nEXPIRES_IN=" + ttl + "\r\n";
    }

    synchronized Status status(String ticket) {
        if (!ticket.equals(current)) {
            return Status.UNKNOWN;
        }
        return System.nanoTime() - expiresAt >= 0 ? Status.EXPIRED : Status.ACTIVE;
    }

    synchronized void reset(int ttl) {
        this.ttl = ttl;
        current = null;
    }
}
