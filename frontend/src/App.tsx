import { Navigate, Route, Routes } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { apiGet, type StatusResponse } from "./lib/api";
import LoginPage from "./pages/LoginPage";
import DashboardLayout from "./pages/DashboardLayout";
import LivePage from "./pages/LivePage";
import HistoryPage from "./pages/HistoryPage";

function useStatus(intervalMs = 4000) {
  return useQuery({
    queryKey: ["status"],
    queryFn: () => apiGet<StatusResponse>("/api/status"),
    refetchInterval: intervalMs,
  });
}

function RequireConnected({ children }: { children: JSX.Element }) {
  const { data, isLoading } = useStatus(5000);
  if (isLoading) return <FullScreenLoader />;
  if (!data?.connected) return <Navigate to="/login" replace />;
  return children;
}

function RedirectIfConnected({ children }: { children: JSX.Element }) {
  const { data, isLoading } = useStatus(5000);
  if (isLoading) return <FullScreenLoader />;
  if (data?.connected) return <Navigate to="/dashboard/live" replace />;
  return children;
}

function FullScreenLoader() {
  return (
    <div className="flex h-screen items-center justify-center text-slate-400">
      <div className="animate-pulse">Loading…</div>
    </div>
  );
}

export default function App() {
  return (
    <Routes>
      <Route
        path="/login"
        element={
          <RedirectIfConnected>
            <LoginPage />
          </RedirectIfConnected>
        }
      />
      <Route
        path="/dashboard"
        element={
          <RequireConnected>
            <DashboardLayout />
          </RequireConnected>
        }
      >
        <Route index element={<Navigate to="live" replace />} />
        <Route path="live" element={<LivePage />} />
        <Route path="history" element={<HistoryPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/dashboard/live" replace />} />
    </Routes>
  );
}
