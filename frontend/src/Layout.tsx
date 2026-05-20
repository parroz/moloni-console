import { NavLink } from "react-router-dom";
import { logout } from "./api";

export default function Layout({ children }: { children: React.ReactNode }) {
  return (
    <div className="layout">
      <header className="topnav no-print">
        <NavLink to="/invoices" className="topnav-brand">
          <strong>Moloni console</strong>
        </NavLink>
        <NavLink to="/invoices" className={({ isActive }) => (isActive ? "active" : "")}>
          Faturas fornecedor
        </NavLink>
        <NavLink to="/products" className={({ isActive }) => (isActive ? "active" : "")}>
          Produtos
        </NavLink>
        <NavLink to="/suppliers" className={({ isActive }) => (isActive ? "active" : "")}>
          Fornecedores
        </NavLink>
        <NavLink to="/categories" className={({ isActive }) => (isActive ? "active" : "")}>
          Categorias
        </NavLink>
        <NavLink to="/agent" className={({ isActive }) => (isActive ? "active" : "")}>
          Agente
        </NavLink>
        <NavLink to="/agent/suppliers" className={({ isActive }) => (isActive ? "active" : "")}>
          Fornecedores IA
        </NavLink>
        <span className="topnav-spacer" />
        <button
          type="button"
          className="btn"
          onClick={async () => {
            await logout();
            window.location.href = "/login";
          }}
        >
          Sair
        </button>
      </header>
      <main>{children}</main>
    </div>
  );
}
