package com.brightwater.halden;

import java.io.IOException;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.Locale;
import javax.servlet.http.HttpServletResponse;

final class LegacyCodec {
    static final String INTERNAL_ERROR = "<html><body><h1>HALDEN ERP - Internal Error</h1></body></html>";
    static final String JSON = "application/json";
    static final String TEXT = "text/plain";
    static final String HTML = "text/html";
    private static final String HEX = "0123456789abcdef";

    private LegacyCodec() {
    }

    static void send(HttpServletResponse response, int status, String type, String body) throws IOException {
        response.setStatus(status);
        response.setContentType(type);
        response.setCharacterEncoding("UTF-8");
        response.getWriter().write(body);
    }

    static void internalError(HttpServletResponse response) throws IOException {
        response.resetBuffer();
        send(response, 500, HTML, INTERNAL_ERROR);
    }

    static String json(String value) {
        if (value == null) {
            return "null";
        }
        StringBuilder out = new StringBuilder().append('"');
        for (int i = 0; i < value.length(); i++) {
            char character = value.charAt(i);
            switch (character) {
                case '"': out.append("\\\""); break;
                case '\\': out.append("\\\\"); break;
                case '\b': out.append("\\b"); break;
                case '\f': out.append("\\f"); break;
                case '\n': out.append("\\n"); break;
                case '\r': out.append("\\r"); break;
                case '\t': out.append("\\t"); break;
                default:
                    if (character < 0x20) {
                        out.append("\\u00").append(HEX.charAt(character >> 4)).append(HEX.charAt(character & 15));
                    } else {
                        out.append(character);
                    }
            }
        }
        return out.append('"').toString();
    }

    static void xmlElement(StringBuilder out, String name, String value) {
        if (value == null) {
            return;
        }
        out.append('<').append(name).append('>');
        for (int i = 0; i < value.length(); i++) {
            char character = value.charAt(i);
            switch (character) {
                case '&': out.append("&amp;"); break;
                case '<': out.append("&lt;"); break;
                case '>': out.append("&gt;"); break;
                case '"': out.append("&quot;"); break;
                case '\'': out.append("&apos;"); break;
                default: out.append(character);
            }
        }
        out.append("</").append(name).append('>');
    }

    static String csv(String value) {
        return value == null ? "" : value;
    }

    static BigDecimal legacyPrice(String raw) {
        if (raw == null) {
            return null;
        }
        String price = raw.replace("€", "").replace("EUR", "").replace(" ", "");
        if (price.contains(".") && price.contains(",")) {
            price = price.replace(".", "");
        }
        try {
            return new BigDecimal(price.replace(',', '.'));
        } catch (NumberFormatException ignored) {
            return null;
        }
    }

    static int legacyInt(String raw) {
        Integer value = raw == null ? null : integer(raw.trim().replace(".", ""));
        return value == null ? 0 : value;
    }

    static Integer integer(String value) {
        if (value == null) {
            return null;
        }
        try {
            return Integer.parseInt(value.trim());
        } catch (NumberFormatException ignored) {
            return null;
        }
    }

    static int intOrDefault(String value, int minimum, int fallback) {
        Integer parsed = integer(value);
        return parsed == null || parsed < minimum ? fallback : parsed;
    }

    static boolean legacyTruthy(String value) {
        if (value == null) {
            return false;
        }
        switch (value.trim().toUpperCase(Locale.ROOT)) {
            case "J": case "Y": case "1": case "TRUE": return true;
            default: return false;
        }
    }

    static String money(BigDecimal amount) {
        return amount.setScale(2, RoundingMode.HALF_UP).toPlainString();
    }
}
