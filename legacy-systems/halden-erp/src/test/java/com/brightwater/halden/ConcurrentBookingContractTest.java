package com.brightwater.halden;

import static com.brightwater.halden.ContractTestSupport.*;
import static org.junit.Assert.*;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.CyclicBarrier;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.regex.Pattern;
import org.junit.Test;

public class ConcurrentBookingContractTest {
    private static final String ITEM = "BW-1010";
    private static final String ORPHANS =
            "SELECT COUNT(*) FROM ORDLIN l LEFT JOIN ORDHDR h ON l.ORDNO=h.ORDNO WHERE h.ORDNO IS NULL";

    @Test
    public void ga20_concurrentLastStockHasTwentyOverlapWitnesses() throws Exception {
        for (int iteration = 1; iteration <= 20; iteration++) {
            runIteration(iteration);
        }
    }

    private static void runIteration(int iteration) throws Exception {
        Probe probe = new Probe();
        try (Fixture server = Fixture.start(true, probe)) {
            server.reset("rate_limit", "0");
            String ticket = server.login();
            Applicant a = new Applicant(server, ticket, "00417", "417", "RACE-A");
            Applicant b = new Applicant(server, ticket, "52290", "52290", "RACE-B");
            int allocatedBefore = allocated(server, ticket);
            assertEquals(0, allocatedBefore);
            assertEquals(10, server.orderCount(ticket));

            CyclicBarrier release = new CyclicBarrier(2);
            ExecutorService callers = Executors.newFixedThreadPool(2);
            Response responseA;
            Response responseB;
            try {
                Future<Response> futureA = callers.submit(() -> book(server, ticket, release, a));
                Future<Response> futureB = callers.submit(() -> book(server, ticket, release, b));
                responseA = futureA.get(10, TimeUnit.SECONDS);
                responseB = futureB.get(10, TimeUnit.SECONDS);
            } finally {
                callers.shutdownNow();
            }

            List<Arrival> arrivals = new ArrayList<>(probe.arrivals);
            boolean overlap = arrivals.size() == 2 && probe.passed.get() == 2
                    && !arrivals.get(0).thread.equals(arrivals.get(1).thread)
                    && (arrivals.get(0).ref.equals(a.ref) && arrivals.get(1).ref.equals(b.ref)
                        || arrivals.get(0).ref.equals(b.ref) && arrivals.get(1).ref.equals(a.ref));
            assertTrue("iteration=" + iteration + " overlap=" + overlap + " arrivals=" + arrivals, overlap);

            boolean aWon = responseA.status == 200;
            Applicant winner = aWon ? a : b;
            Applicant loser = aWon ? b : a;
            Response won = aWon ? responseA : responseB;
            Response lost = aWon ? responseB : responseA;
            won.status(200).type("application/json").contains("\"rc\":0");
            lost.status(409).type("text/plain").body("SHORT:BW-1010:0");
            String orderNo = group(Pattern.compile("\"order_no\":\"(SO-[0-9]{6})\""), won.body);
            Response readBack = server.get(ORDERS + "/" + orderNo, ticket).status(200).type("application/json");
            boolean winnerMatches = readBack.body.contains("\"OrderNo\":\"" + orderNo + "\"")
                    && readBack.body.contains("\"Acct\":\"" + winner.wireAccount + "\"")
                    && readBack.body.contains("\"Ref\":\"" + winner.ref + "\"")
                    && readBack.body.contains("\"lines\":[{\"ln\":1,\"item\":\"BW-1010\",\"qty\":\"2\",\"price\":\"118.50\"}]");
            assertTrue(readBack.body, winnerMatches);

            int allocatedAfter = allocated(server, ticket);
            assertEquals(2, allocatedAfter);
            assertEquals(11, server.orderCount(ticket));
            boolean loserOrdersUnchanged = loser.orders.equals(server.orderPages(ticket, loser.account, false));
            boolean loserCustomerUnchanged = loser.row.equals(server.customerRow(loser.account));
            assertTrue("Loser E6 order list changed", loserOrdersUnchanged);
            assertTrue("Loser CUSTMAST row changed", loserCustomerUnchanged);
            assertEquals("Loser E3 row changed", loser.customer, server.customer(ticket, loser.account));
            int orphans = server.scalar(ORPHANS);
            int lines = server.scalar("SELECT COUNT(*) FROM ORDLIN");
            assertEquals(0, orphans);
            assertEquals(11, lines);
            System.err.println("GA20-WITNESS iteration=" + iteration + " arrivals=" + arrivals.get(0) + "," + arrivals.get(1)
                    + " overlap=" + overlap + " winner=" + winner.account + "/" + winner.ref + "/" + orderNo
                    + " loser=" + loser.account + "/" + loser.ref + " loser_response=" + lost.status + ":" + lost.body
                    + " allocated_before=" + allocatedBefore + " allocated_after=" + allocatedAfter + " orphans=" + orphans
                    + " ordlin=" + lines + " loser_custmast_unchanged=" + loserCustomerUnchanged
                    + " loser_orders_unchanged=" + loserOrdersUnchanged + " winner_e7_matches=" + winnerMatches);
        }
    }

    private static Response book(Fixture server, String ticket, CyclicBarrier release, Applicant booking)
            throws Exception {
        release.await(5, TimeUnit.SECONDS);
        return server.order(ticket, booking.account, booking.ref, ITEM, "2");
    }

    private static int allocated(Fixture server, String ticket) throws Exception {
        String[] rows = server.stock(ticket, ITEM).split("\\r\\n");
        assertEquals(2, rows.length);
        assertEquals("ITEM;WH;ON_HAND;ALLOCATED", rows[0]);
        assertTrue(rows[1], rows[1].startsWith("BW-1010;DUS;2;"));
        return Integer.parseInt(rows[1].split(";")[3]);
    }

    private static final class Applicant {
        final String account;
        final String wireAccount;
        final String ref;
        final List<List<String>> row;
        final String customer;
        final List<String> orders;

        Applicant(Fixture server, String ticket, String account, String wireAccount, String ref) throws Exception {
            this.account = account;
            this.wireAccount = wireAccount;
            this.ref = ref;
            row = server.customerRow(account);
            assertEquals(1, row.size());
            customer = server.customer(ticket, account);
            assertTrue(customer, customer.contains("\"acct\":" + wireAccount + ","));
            orders = server.orderPages(ticket, account, false);
            assertTrue(Fixture.countOrders(orders) > 0);
        }
    }

    private static final class Probe implements OrderBook.BookingProbe {
        final ConcurrentLinkedQueue<Arrival> arrivals = new ConcurrentLinkedQueue<>();
        final CountDownLatch rendezvous = new CountDownLatch(2);
        final AtomicInteger passed = new AtomicInteger();

        @Override
        public void beforeWriteLock(String ref) {
            arrivals.add(new Arrival(Thread.currentThread().getName(), ref, System.nanoTime()));
            rendezvous.countDown();
            try {
                if (rendezvous.await(5, TimeUnit.SECONDS)) {
                    passed.incrementAndGet();
                }
            } catch (InterruptedException interruption) {
                Thread.currentThread().interrupt();
                throw new IllegalStateException("Interrupted while observing booking overlap", interruption);
            }
        }
    }

    private static final class Arrival {
        final String thread;
        final String ref;
        final long nanos;

        Arrival(String thread, String ref, long nanos) {
            this.thread = thread;
            this.ref = ref;
            this.nanos = nanos;
        }

        @Override
        public String toString() {
            return thread + ":" + ref + ":" + nanos;
        }
    }
}
