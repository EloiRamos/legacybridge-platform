import { describe, expect, it } from "vitest";

import { ApiError } from "./api/client";
import { outcomeOf } from "./outcome";

const problem = (status: number, code: string) => new ApiError({ status, code, title: "t", detail: "d" });

// RR-1: a failed command may only be called refused when the server itself said so.
describe("outcomeOf", () => {
  it.each([[401, "UNAUTHENTICATED"], [403, "FORBIDDEN"], [409, "CONFLICT_STATE"], [422, "VALIDATION"], [429, "RATE_LIMITED"]])(
    "%i %s is a definite refusal", (s, c) => expect(outcomeOf(problem(s, c))).toBe("refused"));
  it.each([[500, "INTERNAL"], [502, "BACKEND_MALFORMED"], [503, "DATABASE_UNAVAILABLE"], [503, "BACKEND_UNAVAILABLE"],
    [408, "TIMEOUT"], [404, "HTTP_404"], [400, "HTTP_400"], [302, "HTTP_302"]])(
    "%i %s leaves the outcome unknown", (s, c) => expect(outcomeOf(problem(s, c))).toBe("unknown"));
  it.each([new TypeError("Failed to fetch"), new SyntaxError("bad json"), undefined, "x"])(
    "a lost or unreadable response leaves the outcome unknown", (e) => expect(outcomeOf(e)).toBe("unknown"));
});
