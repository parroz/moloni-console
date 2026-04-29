import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { apiJson } from "../api";

type Cat = {
  category_id: number;
  parent_id: number;
  name: string;
  description?: string;
  pos_enabled?: number;
  num_products?: number;
};

function CategoryBranch({ cat }: { cat: Cat }) {
  const [opened, setOpened] = useState(false);
  const q = useQuery({
    queryKey: ["categories", "level", cat.category_id],
    queryFn: () => apiJson<Cat[]>(`/moloni/categories?parent_id=${cat.category_id}`),
    enabled: opened,
  });

  return (
    <details
      className="cat-tree"
      onToggle={(e) => setOpened((e.target as HTMLDetailsElement).open)}
    >
      <summary>
        <strong>{cat.name}</strong>
        <span className="muted">
          {" "}
          #{cat.category_id}
          {cat.num_products != null ? ` · ${cat.num_products} art.` : ""}
        </span>
      </summary>
      <div className="cat-tree-children">
        {opened && q.isFetching ? <p className="muted">A carregar subcategorias…</p> : null}
        {opened && q.error ? <p className="error">{(q.error as Error).message}</p> : null}
        {opened && q.data && q.data.length === 0 ? <p className="muted">Sem filhos.</p> : null}
        {opened && (q.data ?? []).map((k) => (
          <CategoryBranch key={k.category_id} cat={k} />
        ))}
      </div>
    </details>
  );
}

export default function CategoriesPage() {
  const qc = useQueryClient();
  const rootsQ = useQuery({
    queryKey: ["categories", "roots"],
    queryFn: () => apiJson<Cat[]>("/moloni/categories?parent_id=0"),
  });

  const [newParent, setNewParent] = useState(0);
  const [newName, setNewName] = useState("");
  const [edits, setEdits] = useState<Record<number, { name: string; parent_id: number; description: string }>>({});

  const createMut = useMutation({
    mutationFn: () =>
      apiJson("/moloni/categories", {
        method: "POST",
        body: JSON.stringify({ parent_id: newParent, name: newName, description: "", pos_enabled: 1 }),
      }),
    onSuccess: () => {
      setNewName("");
      qc.invalidateQueries({ queryKey: ["categories"] });
    },
  });

  const saveMut = useMutation({
    mutationFn: (c: Cat & { name: string; parent_id: number; description: string }) =>
      apiJson(`/moloni/categories/${c.category_id}`, {
        method: "PUT",
        body: JSON.stringify({
          parent_id: c.parent_id,
          name: c.name,
          description: c.description || "",
          pos_enabled: c.pos_enabled ?? 1,
        }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["categories"] }),
  });

  const delMut = useMutation({
    mutationFn: (category_id: number) => apiJson(`/moloni/categories/${category_id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["categories"] }),
  });

  function baseEdit(c: Cat) {
    return (
      edits[c.category_id] ?? {
        name: c.name,
        parent_id: c.parent_id,
        description: c.description || "",
      }
    );
  }

  const flatForTable = rootsQ.data ?? [];

  return (
    <div>
      <h1>Categorias</h1>
      <p className="muted">
        Carregamento <strong>rápido</strong>: primeiro só o nível raiz (<code>parent_id=0</code>). Ao expandir um
        ramo, pede-se <code>GET /api/moloni/categories?parent_id=…</code> (equivalente ao POST Moloni com{" "}
        <code>json=true</code> no backend).
      </p>
      {rootsQ.isLoading ? <p>A carregar raízes…</p> : null}
      {rootsQ.error ? <p className="error">{(rootsQ.error as Error).message}</p> : null}
      {rootsQ.data && rootsQ.data.length === 0 ? (
        <p className="error">Nenhuma categoria de topo. Confirme MOLONI_COMPANY_ID.</p>
      ) : null}

      {rootsQ.data && rootsQ.data.length > 0 ? (
        <div className="card">
          <h2>Árvore</h2>
          <p className="muted no-print">Expanda cada ramo para ir buscando os filhos ao Moloni.</p>
          {rootsQ.data.map((c) => (
            <CategoryBranch key={c.category_id} cat={c} />
          ))}
        </div>
      ) : null}

      <div className="card no-print">
        <h2>Nova categoria</h2>
        <div className="form-grid" style={{ alignItems: "end" }}>
          <label>
            parent_id (0 = raiz; use o # mostrado na árvore)
            <input type="number" value={newParent} onChange={(e) => setNewParent(Number(e.target.value))} />
          </label>
          <label>
            Nome
            <input value={newName} onChange={(e) => setNewName(e.target.value)} />
          </label>
          <button type="button" className="btn primary" disabled={!newName || createMut.isPending} onClick={() => createMut.mutate()}>
            Criar
          </button>
        </div>
        {createMut.error ? <p className="error">{(createMut.error as Error).message}</p> : null}
      </div>

      {flatForTable.length > 0 ? (
        <div className="card no-print" style={{ padding: 0, overflow: "auto" }}>
          <h2 style={{ padding: "0.75rem 1rem 0", margin: 0 }}>Raízes (edição rápida)</h2>
          <p className="muted" style={{ padding: "0 1rem", marginTop: 0 }}>
            Só as categorias de topo aparecem aqui; use o Moloni ou a API para editar nós profundos, ou expanda a
            árvore e copie o <code>parent_id</code>.
          </p>
          <table className="data">
            <thead>
              <tr>
                <th>ID</th>
                <th>parent_id</th>
                <th>Nome</th>
                <th>Descrição</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {flatForTable.map((c) => {
                const cur = baseEdit(c);
                return (
                  <tr key={c.category_id}>
                    <td>{c.category_id}</td>
                    <td>
                      <input
                        type="number"
                        value={cur.parent_id}
                        onChange={(e) =>
                          setEdits((m) => {
                            const prev = m[c.category_id] ?? {
                              name: c.name,
                              parent_id: c.parent_id,
                              description: c.description || "",
                            };
                            return { ...m, [c.category_id]: { ...prev, parent_id: Number(e.target.value) } };
                          })
                        }
                      />
                    </td>
                    <td>
                      <input
                        value={cur.name}
                        onChange={(e) =>
                          setEdits((m) => {
                            const prev = m[c.category_id] ?? {
                              name: c.name,
                              parent_id: c.parent_id,
                              description: c.description || "",
                            };
                            return { ...m, [c.category_id]: { ...prev, name: e.target.value } };
                          })
                        }
                      />
                    </td>
                    <td>
                      <input
                        value={cur.description}
                        onChange={(e) =>
                          setEdits((m) => {
                            const prev = m[c.category_id] ?? {
                              name: c.name,
                              parent_id: c.parent_id,
                              description: c.description || "",
                            };
                            return { ...m, [c.category_id]: { ...prev, description: e.target.value } };
                          })
                        }
                      />
                    </td>
                    <td>
                      <button
                        type="button"
                        className="btn"
                        disabled={saveMut.isPending}
                        onClick={() =>
                          saveMut.mutate({
                            ...c,
                            name: edits[c.category_id]?.name ?? c.name,
                            parent_id: edits[c.category_id]?.parent_id ?? c.parent_id,
                            description: edits[c.category_id]?.description ?? (c.description || ""),
                          })
                        }
                      >
                        Guardar
                      </button>{" "}
                      <button
                        type="button"
                        className="btn"
                        disabled={delMut.isPending}
                        onClick={() => {
                          if (window.confirm(`Eliminar categoria #${c.category_id}?`)) delMut.mutate(c.category_id);
                        }}
                      >
                        Apagar
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}
