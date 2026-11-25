import createClient from "openapi-fetch";

import type { components, paths } from "./schema";

export type Schemas = components["schemas"];

// The CSRF synchronizer token lives in memory only (never localStorage/sessionStorage, SEC-08).
let csrfToken: string | null = null;
export function setCsrf(token: string | null): void {
  csrfToken = token;
}

export const api = createClient<paths>({ baseUrl: "", credentials: "same-origin" });
api.use({
  onRequest({ request }) {
    if (request.method !== "GET" && csrfToken) request.headers.set("X-CSRF-Token", csrfToken);
    return request;
  },
});

export interface Problem {
  status: number;
  code: string;
  title: string;
  detail: string;
  request_id?: string;
}

export class ApiError extends Error {
  readonly problem: Problem;
  constructor(problem: Problem) {
    super(problem.detail || problem.title || problem.code);
    this.problem = problem;
  }
}

/** Unwraps an openapi-fetch result; a problem (RFC 9457) becomes ApiError, whatever its status: the assistant reports
 * ASSISTANT_UNAVAILABLE with 200, which is not a turn. */
export async function call<T>(p: Promise<{ data?: T; error?: unknown; response: Response }>): Promise<Exclude<T, { code: "ASSISTANT_UNAVAILABLE" }>> {
  const { data, error, response } = await p;
  if (!response.ok || response.headers.get("content-type")?.startsWith("application/problem+json")) {
    const e = (error ?? data ?? {}) as Partial<Problem>;
    throw new ApiError({
      status: response.status,
      code: e.code ?? "HTTP_" + response.status,
      title: e.title ?? response.statusText,
      detail: e.detail ?? "",
      request_id: e.request_id,
    });
  }
  return data as Exclude<T, { code: "ASSISTANT_UNAVAILABLE" }>;
}
