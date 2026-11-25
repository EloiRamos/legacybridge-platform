package com.brightwater.halden;

import java.io.IOException;
import javax.servlet.http.HttpServlet;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

final class PingServlet extends HttpServlet {
    static final String READY = "HALDEN-ERP 3.1 READY";

    @Override
    protected void doGet(HttpServletRequest request, HttpServletResponse response) throws IOException {
        LegacyCodec.send(response, 200, LegacyCodec.TEXT, READY);
    }
}
