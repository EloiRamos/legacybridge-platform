package com.brightwater.halden;

import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;

final class FaultRegistry {
    static final class Fault {
        final String method;
        final String prefix;
        final String mode;
        final int delayMs;
        int remaining;

        Fault(String method, String prefix, String mode, int count, int delayMs) {
            this.method = method;
            this.prefix = prefix;
            this.mode = mode;
            this.delayMs = delayMs;
            remaining = count;
        }
    }

    private final List<Fault> faults = new ArrayList<>();

    synchronized int arm(Fault fault) {
        faults.add(fault);
        return faults.size();
    }

    synchronized Fault consume(String method, String uri) {
        Iterator<Fault> iterator = faults.iterator();
        while (iterator.hasNext()) {
            Fault fault = iterator.next();
            if (fault.method.equals(method) && uri.startsWith(fault.prefix)) {
                if (--fault.remaining == 0) {
                    iterator.remove();
                }
                return fault;
            }
        }
        return null;
    }

    synchronized String list() {
        StringBuilder body = new StringBuilder("{\"faults\":[");
        String separator = "";
        for (Fault fault : faults) {
            body.append(separator).append("{\"method\":").append(LegacyCodec.json(fault.method))
                    .append(",\"path_prefix\":").append(LegacyCodec.json(fault.prefix))
                    .append(",\"mode\":").append(LegacyCodec.json(fault.mode))
                    .append(",\"remaining\":").append(fault.remaining)
                    .append(",\"delay_ms\":").append(fault.delayMs).append('}');
            separator = ",";
        }
        return body.append("]}").toString();
    }

    synchronized void clear() {
        faults.clear();
    }
}
