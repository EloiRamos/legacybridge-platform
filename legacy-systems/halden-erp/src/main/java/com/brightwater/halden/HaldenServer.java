package com.brightwater.halden;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Comparator;
import java.util.List;
import java.util.logging.Level;
import java.util.logging.Logger;
import java.util.stream.Collectors;
import java.util.stream.Stream;
import javax.servlet.http.HttpServlet;
import org.apache.catalina.Context;
import org.apache.catalina.connector.Connector;
import org.apache.catalina.startup.Tomcat;
import org.apache.tomcat.util.descriptor.web.FilterDef;
import org.apache.tomcat.util.descriptor.web.FilterMap;

public final class HaldenServer implements AutoCloseable {
    private static final Logger LOG = Logger.getLogger(HaldenServer.class.getName());
    private final Tomcat tomcat;
    private final Connector connector;
    private final Path baseDir;
    private boolean stopped;

    private HaldenServer(Tomcat tomcat, Connector connector, Path baseDir) {
        this.tomcat = tomcat;
        this.connector = connector;
        this.baseDir = baseDir;
    }

    static HaldenServer start(HaldenConfig config) throws Exception {
        return start(config, OrderBook.NO_PROBE);
    }

    static HaldenServer start(HaldenConfig config, OrderBook.BookingProbe probe) throws Exception {
        Database db = new Database(config.dbPath);
        db.initialize(config.resetOnStart);
        TicketRegistry tickets = new TicketRegistry(config);
        RateLimiter rate = new RateLimiter(config.rateLimit, config.rateWindow);
        FaultRegistry faults = new FaultRegistry();
        OrderBook orders = new OrderBook(db, probe);

        Path baseDir = Files.createTempDirectory("halden-tomcat");
        Tomcat tomcat = new Tomcat();
        tomcat.setBaseDir(baseDir.toString());
        tomcat.setPort(config.port);
        Connector connector = tomcat.getConnector();
        connector.setURIEncoding("UTF-8");
        HaldenServer server = new HaldenServer(tomcat, connector, baseDir);
        try {
            Context context = tomcat.addContext("", baseDir.toString());
            FilterDef filter = new FilterDef();
            filter.setFilterName("gateway");
            filter.setFilterClass(GatewayFilter.class.getName());
            filter.setFilter(new GatewayFilter(tickets, rate, faults));
            context.addFilterDef(filter);
            FilterMap mapping = new FilterMap();
            mapping.setFilterName("gateway");
            mapping.addURLPattern("/*");
            context.addFilterMap(mapping);

            servlet(context, "ping", new PingServlet(), "/halden/ping");
            servlet(context, "session", new SessionServlet(tickets), "/halden/session");
            servlet(context, "customer", new CustomerServlet(db), "/halden/customers");
            servlet(context, "item", new ItemServlet(db), "/halden/items");
            servlet(context, "stock", new StockServlet(db), "/halden/stock");
            servlet(context, "order", new OrderServlet(db, orders), "/halden/orders", "/halden/orders/*");
            servlet(context, "sim", new SimControlServlet(config, db, tickets, rate, faults, orders), "/_sim/*");
            tomcat.start();
            if (!context.getState().isAvailable() || connector.getLocalPort() < 0) {
                throw new IllegalStateException("HALDEN Tomcat context or connector failed to start");
            }
            return server;
        } catch (Exception failure) {
            try {
                server.stop();
            } catch (Exception cleanupFailure) {
                failure.addSuppressed(cleanupFailure);
            }
            throw failure;
        }
    }

    private static void servlet(Context context, String name, HttpServlet servlet, String... paths) {
        Tomcat.addServlet(context, name, servlet);
        for (String path : paths) {
            context.addServletMappingDecoded(path, name);
        }
    }

    int port() {
        return connector.getLocalPort();
    }

    synchronized void stop() throws Exception {
        if (stopped) {
            return;
        }
        stopped = true;
        try {
            tomcat.stop();
        } finally {
            try {
                tomcat.destroy();
            } finally {
                deleteTree(baseDir);
            }
        }
    }

    private static void deleteTree(Path root) throws IOException {
        List<Path> paths;
        try (Stream<Path> tree = Files.walk(root)) {
            paths = tree.sorted(Comparator.reverseOrder()).collect(Collectors.toList());
        }
        for (Path path : paths) {
            Files.deleteIfExists(path);
        }
    }

    @Override
    public void close() throws Exception {
        stop();
    }

    public static void main(String[] args) throws Exception {
        HaldenServer server = start(HaldenConfig.fromEnv());
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            try {
                server.stop();
            } catch (Exception failure) {
                LOG.log(Level.WARNING, "Could not stop HALDEN", failure);
            }
        }, "halden-shutdown"));
        server.tomcat.getServer().await();
    }
}
