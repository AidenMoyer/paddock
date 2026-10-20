// Small, safe Markdown renderer (escapes everything first; only whitelisted constructs become HTML).
// Item references like #123 become links to that item.

import { esc } from "./ui.js";

function inline(s) {
  s = esc(s);
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  s = s.replace(/(^|[\s(\[|,])#(\d{1,9})\b/g, '$1<a class="ref" href="/go/$2">#$2</a>');
  s = s.replace(/\[([^\]]+)\]\((\/[^)\s]*)\)/g, '<a class="ref" href="$2">$1</a>'); // internal links only
  return s;
}

export function renderMarkdown(src) {
  const lines = String(src || "").replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let i = 0;
  const listStack = [];
  const closeLists = () => { while (listStack.length) out.push(`</${listStack.pop()}>`); };
  while (i < lines.length) {
    const line = lines[i];
    if (/^\s*$/.test(line)) { closeLists(); i++; continue; }
    if (/^```/.test(line)) {
      closeLists();
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++;
      out.push(`<pre class="code">${esc(buf.join("\n"))}</pre>`);
      continue;
    }
    const hm = line.match(/^(#{1,4})\s+(.*)$/);
    if (hm) { closeLists(); const n = Math.min(hm[1].length + 1, 4); out.push(`<h${n}>${inline(hm[2])}</h${n}>`); i++; continue; }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      closeLists();
      const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
      out.push("<table><thead><tr>" + cells(line).map((c) => `<th>${c}</th>`).join("") + "</tr></thead><tbody>");
      i += 2;
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) out.push("<tr>" + cells(lines[i++]).map((c) => `<td>${c}</td>`).join("") + "</tr>");
      out.push("</tbody></table>");
      continue;
    }
    const lm = line.match(/^(\s*)([-*+]|\d+[.)])\s+(\[[ xX]\]\s+)?(.*)$/);
    if (lm) {
      const type = /\d/.test(lm[2]) ? "ol" : "ul";
      if (!listStack.length || listStack[listStack.length - 1] !== type) { closeLists(); out.push(`<${type}>`); listStack.push(type); }
      const box = lm[3] ? `<input type="checkbox" disabled ${/x/i.test(lm[3]) ? "checked" : ""}>` : "";
      out.push(`<li>${box}${inline(lm[4])}</li>`);
      i++;
      continue;
    }
    if (/^>\s?/.test(line)) { closeLists(); out.push(`<blockquote>${inline(line.replace(/^>\s?/, ""))}</blockquote>`); i++; continue; }
    if (/^(-{3,}|\*{3,})$/.test(line.trim())) { closeLists(); out.push("<hr>"); i++; continue; }
    closeLists();
    const para = [line];
    i++;
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|```|\s*[-*+]\s|\s*\d+[.)]\s|>|\s*\|)/.test(lines[i])) para.push(lines[i++]);
    out.push(`<p>${para.map(inline).join("<br>")}</p>`);
  }
  closeLists();
  return out.join("\n");
}
