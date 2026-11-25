import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, type ReactNode, useContext } from "react";

import { api, ApiError, call, type Schemas, setCsrf } from "./api/client";

type Session = Schemas["SessionOut"];

interface SessionState {
  session: Session | null;
  loading: boolean;
  can: (permission: string) => boolean;
  refresh: () => Promise<void>;
  signIn: (s: Session) => void;
}

const Ctx = createContext<SessionState | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["session"],
    queryFn: async () => {
      try {
        const s = await call(api.GET("/api/v1/auth/session"));
        setCsrf(s.csrf_token);
        return s;
      } catch (e) {
        if (e instanceof ApiError && e.problem.status === 401) {
          setCsrf(null);
          return null;
        }
        throw e;
      }
    },
    staleTime: Infinity,
    retry: false,
  });
  const session = q.data ?? null;
  const value: SessionState = {
    session,
    loading: q.isLoading,
    can: (p) => !!session && session.permissions.includes(p as Schemas["Permission"]),
    refresh: async () => {
      await qc.invalidateQueries({ queryKey: ["session"] });
    },
    signIn: (s) => {
      setCsrf(s.csrf_token);
      qc.setQueryData(["session"], s);
    },
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSession(): SessionState {
  const v = useContext(Ctx);
  if (!v) throw new Error("SessionProvider missing");
  return v;
}
