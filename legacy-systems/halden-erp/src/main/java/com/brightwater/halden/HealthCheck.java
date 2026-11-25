package com.brightwater.halden;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

public final class HealthCheck {
    private HealthCheck() {
    }

    public static void main(String[] args) throws IOException {
        HaldenConfig config = HaldenConfig.fromEnv();
        HttpURLConnection connection = (HttpURLConnection)
                new URL("http://127.0.0.1:" + config.port + "/halden/ping").openConnection();
        connection.setConnectTimeout(2000);
        connection.setReadTimeout(2000);
        try {
            if (connection.getResponseCode() != 200) {
                throw new IOException("HALDEN ping failed");
            }
            try (InputStream input = connection.getInputStream()) {
                ByteArrayOutputStream bytes = new ByteArrayOutputStream();
                byte[] chunk = new byte[128];
                int count;
                while ((count = input.read(chunk)) != -1) {
                    bytes.write(chunk, 0, count);
                }
                if (!PingServlet.READY.equals(new String(bytes.toByteArray(), StandardCharsets.UTF_8))) {
                    throw new IOException("HALDEN ping body did not match");
                }
            }
        } finally {
            connection.disconnect();
        }
    }
}
