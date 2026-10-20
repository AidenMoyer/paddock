// /party/CODE — an invite link: open the party's video with the party joined.
import { api } from "../api.js";
import { navigate } from "../app.js";
import { emptyState } from "../ui.js";

export async function render(root, { match }) {
  const code = match[1].toUpperCase();
  try {
    const p = await api.get(`/api/parties/${code}`, { quiet: true });
    navigate(`/watch/${p.item_id}?party=${p.id}`, { replace: true });
  } catch {
    root.append(emptyState("users", "This watch party has ended", "Ask the host for a new invite, or start your own from any video."));
  }
}
