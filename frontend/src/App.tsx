import { lazy, Suspense, useEffect } from "react";
import { LoaderCircle } from "lucide-react";
import { AppShell } from "./components/AppShell";
import { ToastViewport } from "./components/Toast";
import { useRoute, navigate } from "./lib/router";
import { AppProvider } from "./state/AppContext";
import { ChatView } from "./views/ChatView";

const PlansView = lazy(() => import("./views/PlansView").then((module) => ({ default: module.PlansView })));
const LibraryView = lazy(() => import("./views/LibraryView").then((module) => ({ default: module.LibraryView })));
const SettingsView = lazy(() => import("./views/SettingsView").then((module) => ({ default: module.SettingsView })));
const GraphView = lazy(() => import("./views/GraphView").then((module) => ({ default: module.GraphView })));

function RouteFallback() {
  return (
    <div className="page-loading">
      <LoaderCircle size={22} className="spin" />
      <span>正在加载…</span>
    </div>
  );
}

function NotFoundRedirect() {
  useEffect(() => {
    navigate("/chat", { replace: true });
  }, []);
  return null;
}

function AppRoutes() {
  const route = useRoute();

  let content: React.ReactNode;
  switch (route.name) {
    case "chat":
      content = <ChatView conversationId={route.conversationId} />;
      break;
    case "plans":
      content = <PlansView />;
      break;
    case "library":
      content = <LibraryView query={route.query} />;
      break;
    case "graph":
      content = <GraphView />;
      break;
    case "settings":
      content = <SettingsView />;
      break;
    default:
      content = <NotFoundRedirect />;
  }

  return (
    <AppShell route={route}>
      <Suspense fallback={<RouteFallback />}>{content}</Suspense>
    </AppShell>
  );
}

export default function App() {
  return (
    <AppProvider>
      <AppRoutes />
      <ToastViewport />
    </AppProvider>
  );
}
