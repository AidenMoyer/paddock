// /go/:id — resolves an item reference (e.g. #123 in an AI plan) to the right page.
import { api } from "../api.js";
import { navigate, itemHref } from "../app.js";

export async function render(root, { match }) {
  const item = await api.get(`/api/items/${match[1]}`);
  setTimeout(() => navigate(itemHref(item), { replace: true }), 0);
}
