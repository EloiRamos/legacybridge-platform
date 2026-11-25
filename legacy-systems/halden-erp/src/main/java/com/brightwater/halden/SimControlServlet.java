package com.brightwater.halden;

import java.io.IOException;
import java.sql.SQLException;
import javax.servlet.ServletException;
import javax.servlet.http.HttpServlet;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

final class SimControlServlet extends HttpServlet {
    private final HaldenConfig config;
    private final Database db;
    private final TicketRegistry tickets;
    private final RateLimiter rate;
    private final FaultRegistry faults;
    private final OrderBook orders;

    SimControlServlet(HaldenConfig config, Database db, TicketRegistry tickets, RateLimiter rate,
                      FaultRegistry faults, OrderBook orders) {
        this.config = config;
        this.db = db;
        this.tickets = tickets;
        this.rate = rate;
        this.faults = faults;
        this.orders = orders;
    }

    @Override
    protected void service(HttpServletRequest request, HttpServletResponse response)
            throws IOException, ServletException {
        if (config.simControl) {
            super.service(request, response);
        } else {
            LegacyCodec.send(response, 404, LegacyCodec.TEXT, "NOT FOUND");
        }
    }

    @Override
    protected void doGet(HttpServletRequest request, HttpServletResponse response) throws IOException {
        if ("/faults".equals(request.getPathInfo())) {
            LegacyCodec.send(response, 200, LegacyCodec.JSON, faults.list());
        } else {
            rejectRoute(request, response);
        }
    }

    @Override
    protected void doDelete(HttpServletRequest request, HttpServletResponse response) throws IOException {
        if ("/faults".equals(request.getPathInfo())) {
            faults.clear();
            LegacyCodec.send(response, 200, LegacyCodec.JSON, "{\"cleared\":true}");
        } else {
            rejectRoute(request, response);
        }
    }

    @Override
    protected void doPost(HttpServletRequest request, HttpServletResponse response)
            throws IOException, ServletException {
        String path = request.getPathInfo();
        try {
            if ("/reset".equals(path)) {
                reset(request, response);
            } else if ("/faults".equals(path)) {
                arm(request, response);
            } else if ("/external-order".equals(path)) {
                external(request, response);
            } else {
                rejectRoute(request, response);
            }
        } catch (BadParameter invalid) {
            LegacyCodec.send(response, 400, LegacyCodec.TEXT, "BAD PARAM");
        } catch (SQLException failure) {
            throw new ServletException(failure);
        }
    }

    private void reset(HttpServletRequest request, HttpServletResponse response)
            throws SQLException, IOException, BadParameter {
        int limit = parameter(request, "rate_limit", config.rateLimit, 0, Integer.MAX_VALUE);
        int ttl = parameter(request, "ticket_ttl_sec", config.ticketTtl, 0, Integer.MAX_VALUE);
        int window = parameter(request, "rate_window_sec", config.rateWindow, 1, Integer.MAX_VALUE);
        synchronized (Database.WRITE_LOCK) {
            db.reset();
            faults.clear();
            tickets.reset(ttl);
            rate.reset(limit, window);
            orders.resetExternalSequence();
        }
        LegacyCodec.send(response, 200, LegacyCodec.JSON, "{\"reset\":true,\"rate_limit\":" + limit
                + ",\"ticket_ttl_sec\":" + ttl + ",\"rate_window_sec\":" + window + "}");
    }

    private void arm(HttpServletRequest request, HttpServletResponse response) throws IOException, BadParameter {
        String method = request.getParameter("method");
        String prefix = request.getParameter("path_prefix");
        String mode = request.getParameter("mode");
        if (!("GET".equals(method) || "POST".equals(method)) || prefix == null || !prefix.startsWith("/halden/")
                || !("delay".equals(mode) || "error_500".equals(mode)
                || ("commit_then_500".equals(mode) && "POST".equals(method) && "/halden/orders".equals(prefix)))) {
            throw new BadParameter();
        }
        int count = parameter(request, "count", 1, 1, Integer.MAX_VALUE);
        int delay = parameter(request, "delay_ms", 0, 0, 30000);
        int armed = faults.arm(new FaultRegistry.Fault(method, prefix, mode, count, delay));
        LegacyCodec.send(response, 200, LegacyCodec.JSON, "{\"armed\":" + armed + "}");
    }

    private void external(HttpServletRequest request, HttpServletResponse response) throws SQLException, IOException {
        OrderBook.Result result = orders.externalOrder(request.getParameterValues("acct"),
                request.getParameter("item"), request.getParameter("qty"));
        LegacyCodec.send(response, result.status, result.contentType, result.body);
    }

    private static int parameter(HttpServletRequest request, String key, int fallback, int min, int max)
            throws BadParameter {
        String value = request.getParameter(key);
        if (value == null) {
            return fallback;
        }
        Integer parsed = LegacyCodec.integer(value);
        if (parsed == null || parsed < min || parsed > max) {
            throw new BadParameter();
        }
        return parsed;
    }

    private static void rejectRoute(HttpServletRequest request, HttpServletResponse response) throws IOException {
        String path = request.getPathInfo();
        response.sendError("/reset".equals(path) || "/external-order".equals(path) ? 405 : 404);
    }

    private static final class BadParameter extends Exception {
        private static final long serialVersionUID = 1L;
    }
}
