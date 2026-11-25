package com.brightwater.halden;

import java.io.IOException;
import javax.servlet.http.HttpServlet;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

final class SessionServlet extends HttpServlet {
    private final TicketRegistry tickets;

    SessionServlet(TicketRegistry tickets) {
        this.tickets = tickets;
    }

    @Override
    protected void doPost(HttpServletRequest request, HttpServletResponse response) throws IOException {
        LegacyCodec.send(response, 200, LegacyCodec.TEXT,
                tickets.grant(request.getParameter("login"), request.getParameter("secret")));
    }
}
