package com.brightwater.halden;

import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.TimeUnit;

final class RateLimiter {
    private int limit;
    private long duration;
    private final Map<String, Window> windows = new HashMap<>();

    RateLimiter(int limit, int seconds) {
        reset(limit, seconds);
    }

    synchronized void reset(int limit, int seconds) {
        this.limit = limit;
        duration = TimeUnit.SECONDS.toNanos(seconds);
        windows.clear();
    }

    synchronized long waitSeconds(String ticket) {
        if (limit == 0) {
            return 0;
        }
        long now = System.nanoTime();
        windows.values().removeIf(window -> now - window.deadline >= 0);
        Window window = windows.computeIfAbsent(ticket, key -> new Window(now + duration));
        if (window.used < limit) {
            window.used++;
            return 0;
        }
        return TimeUnit.NANOSECONDS.toSeconds(window.deadline - now - 1) + 1;
    }

    private static final class Window {
        final long deadline;
        int used;

        Window(long deadline) {
            this.deadline = deadline;
        }
    }
}
