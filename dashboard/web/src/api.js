const j = (r) => {
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
};

export const api = {
  runs: () => fetch("/api/runs").then(j),
  state: (id) => fetch(`/api/runs/${id}/state`).then(j),
  bom: (id) => fetch(`/api/runs/${id}/bom`).then(j),
  schematic: (id) =>
    fetch(`/api/runs/${id}/schematic.svg`).then((r) => (r.ok ? r.text() : null)),
  control: (id, body) =>
    fetch(`/api/runs/${id}/control`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(j),
  exportRun: (id) => fetch(`/api/runs/${id}/export`, { method: "POST" }).then(j),
  events: (id) => new EventSource(`/api/runs/${id}/events`),
};
