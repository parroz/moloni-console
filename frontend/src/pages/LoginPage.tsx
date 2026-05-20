import { useEffect, useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { authMe, login } from "../api";

export default function LoginPage() {
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();
  const [boot, setBoot] = useState(true);
  const [already, setAlready] = useState(false);

  useEffect(() => {
    authMe()
      .then((r) => {
        if (r.authenticated) setAlready(true);
      })
      .catch(() => {})
      .finally(() => setBoot(false));
  }, []);

  if (already) {
    return <Navigate to="/" replace />;
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    setLoading(true);
    try {
      await login(password);
      navigate("/");
    } catch (ex) {
      setErr((ex as Error).message || "Falha no login");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="layout">
      <main className="login-shell">
        <div className="card">
          <h1>Entrar</h1>
          <p className="muted">Palavra-passe da consola (definida em CONSOLE_PASSWORD no servidor).</p>
          {boot ? (
            <p className="muted">A verificar sessão…</p>
          ) : (
            <form onSubmit={onSubmit}>
              <label className="login-field">
                <span className="login-field-label">Palavra-passe</span>
                <input
                  className="login-input"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete="current-password"
                />
              </label>
              {err ? <p className="status error">{err}</p> : null}
              <button type="submit" className="btn primary" disabled={loading}>
                {loading ? "A entrar…" : "Entrar"}
              </button>
            </form>
          )}
        </div>
      </main>
    </div>
  );
}
