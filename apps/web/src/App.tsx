import { useEffect } from "react";
import Console from "./components/Console";
import Landing from "./components/Landing";
import { useStore } from "./lib/store";

export default function App() {
  const { caseId, init, toast } = useStore();
  useEffect(() => { init(); }, []);
  return (
    <>
      {caseId ? <Console /> : <Landing />}
      {toast && <div className={`toast ${toast.kind}`} onClick={() => useStore.setState({ toast: null })}>{toast.text}</div>}
    </>
  );
}
