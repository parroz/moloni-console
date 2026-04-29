import { useQuery } from "@tanstack/react-query";
import { Navigate, Outlet, Route, Routes, useNavigate } from "react-router-dom";
import { AuthError, authMe } from "./api";
import CategoriesPage from "./pages/CategoriesPage";
import InvoiceDetailPage from "./pages/InvoiceDetailPage";
import InvoicesPage from "./pages/InvoicesPage";
import LoginPage from "./pages/LoginPage";
import ProductsPage from "./pages/ProductsPage";
import SupplierEditPage from "./pages/SupplierEditPage";
import SuppliersPage from "./pages/SuppliersPage";
import Layout from "./Layout";

function RequireAuth() {
  const navigate = useNavigate();
  const q = useQuery({
    queryKey: ["auth", "me"],
    queryFn: authMe,
    retry: false,
  });

  if (q.isLoading) {
    return (
      <div className="layout">
        <main>
          <p className="muted">A carregar…</p>
        </main>
      </div>
    );
  }
  if (q.error instanceof AuthError || (q.data && !q.data.authenticated)) {
    return <Navigate to="/login" replace />;
  }
  if (q.error) {
    return (
      <div className="layout">
        <main>
          <p className="error">{(q.error as Error).message}</p>
          <button type="button" className="btn" onClick={() => navigate("/login")}>
            Ir para login
          </button>
        </main>
      </div>
    );
  }
  return (
    <Layout>
      <Outlet />
    </Layout>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<RequireAuth />}>
        <Route path="/" element={<InvoicesPage />} />
        <Route path="/invoices" element={<InvoicesPage />} />
        <Route path="/invoices/:documentId" element={<InvoiceDetailPage />} />
        <Route path="/products" element={<ProductsPage />} />
        <Route path="/suppliers" element={<SuppliersPage />} />
        <Route path="/suppliers/:supplierId" element={<SupplierEditPage />} />
        <Route path="/categories" element={<CategoriesPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
