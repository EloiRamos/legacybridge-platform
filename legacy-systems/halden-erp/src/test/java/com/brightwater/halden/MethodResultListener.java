package com.brightwater.halden;

import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import org.junit.runner.Description;
import org.junit.runner.notification.Failure;
import org.junit.runner.notification.RunListener;

public final class MethodResultListener extends RunListener {
    private final Map<Description, Long> starts = new HashMap<>();
    private final Map<Description, String> outcomes = new HashMap<>();

    @Override
    public void testStarted(Description description) {
        starts.put(description, System.nanoTime());
    }

    @Override
    public void testFailure(Failure failure) {
        outcomes.put(failure.getDescription(), failure.getException() instanceof AssertionError ? "FAIL" : "ERROR");
    }

    @Override
    public void testAssumptionFailure(Failure failure) {
        outcomes.put(failure.getDescription(), "SKIPPED");
    }

    @Override
    public void testIgnored(Description description) {
        print(description, "SKIPPED", 0);
    }

    @Override
    public void testFinished(Description description) {
        Long start = starts.remove(description);
        String outcome = outcomes.remove(description);
        long millis = start == null ? 0 : TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);
        print(description, outcome == null ? "PASS" : outcome, millis);
    }

    private static void print(Description description, String outcome, long millis) {
        System.err.println("TEST-RESULT " + description.getClassName() + "#"
                + description.getMethodName() + " " + outcome + " " + millis);
    }
}
