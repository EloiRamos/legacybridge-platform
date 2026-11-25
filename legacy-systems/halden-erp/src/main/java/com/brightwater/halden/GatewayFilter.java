package com.brightwater.halden;

import java.io.IOException;
import java.util.logging.Level;
import java.util.logging.Logger;
import javax.servlet.Filter;
import javax.servlet.FilterChain;
import javax.servlet.ServletException;
import javax.servlet.ServletRequest;
import javax.servlet.ServletResponse;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

final class GatewayFilter implements Filter {
    static final String FAULT_ATTRIBUTE = "halden.fault";
    private static final Logger LOG = Logger.getLogger(GatewayFilter.class.getName());
    private final TicketRegistry tickets;
    private final RateLimiter rate;
    private final FaultRegistry faults;

    GatewayFilter(TicketRegistry tickets, RateLimiter rate, FaultRegistry faults) {
        this.tickets = tickets;
        this.rate = rate;
        this.faults = faults;
    }

    @Override
    public void doFilter(ServletRequest request, ServletResponse response, FilterChain chain)
            throws IOException, ServletException {
        HttpServletRequest req = (HttpServletRequest) request;
        HttpServletResponse res = (HttpServletResponse) response;
        req.setCharacterEncoding("UTF-8");
        res.setCharacterEncoding("UTF-8");
        String uri = req.getRequestURI();
        String path = dispatchPath(req);

        try {
            if (bypassesGateway(path) || allowed(req, res, path)) {
                chain.doFilter(req, res);
            }
        } catch (Throwable failure) {
            LOG.log(Level.WARNING, "HALDEN request failed", failure);
            if (res.isCommitted()) {
                throw new ServletException(failure);
            }
            LegacyCodec.internalError(res);
        } finally {
            LOG.info(req.getMethod() + " " + uri + " " + res.getStatus());
        }
    }

    /**
     * The decoded, normalized path Tomcat used to select the servlet. Classifying on this
     * instead of the raw request URI keeps the gateway decision equal to the dispatch target.
     */
    private static String dispatchPath(HttpServletRequest req) {
        String pathInfo = req.getPathInfo();
        return pathInfo == null ? req.getServletPath() : req.getServletPath() + pathInfo;
    }

    private static boolean bypassesGateway(String path) {
        return path.equals("/halden/ping") || path.equals("/halden/session")
                || path.equals("/_sim") || path.startsWith("/_sim/");
    }

    private boolean allowed(HttpServletRequest req, HttpServletResponse res, String path)
            throws IOException, InterruptedException {
        String ticket = req.getHeader("X-Halden-Ticket");
        if (ticket == null || ticket.trim().isEmpty()) {
            LegacyCodec.send(res, 401, LegacyCodec.TEXT, "NO TICKET");
            return false;
        }
        switch (tickets.status(ticket)) {
            case UNKNOWN:
                LegacyCodec.send(res, 403, LegacyCodec.HTML,
                        "<html><body><h2>403 Forbidden</h2></body></html>");
                return false;
            case EXPIRED:
                LegacyCodec.send(res, 440, LegacyCodec.JSON, "{\"fault\":\"TICKET_EXPIRED\"}");
                return false;
            default:
                break;
        }
        long wait = rate.waitSeconds(ticket);
        if (wait > 0) {
            res.setHeader("X-Halden-Wait", Long.toString(wait));
            LegacyCodec.send(res, 429, LegacyCodec.TEXT, "THROTTLED");
            return false;
        }
        FaultRegistry.Fault fault = faults.consume(req.getMethod(), path);
        if (fault == null) {
            return true;
        }
        switch (fault.mode) {
            case "error_500":
                LegacyCodec.internalError(res);
                return false;
            case "delay":
                Thread.sleep(fault.delayMs);
                return true;
            case "commit_then_500":
                req.setAttribute(FAULT_ATTRIBUTE, fault.mode);
                return true;
            default:
                throw new IllegalStateException("Unknown armed fault mode");
        }
    }
}
