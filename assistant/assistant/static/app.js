"use strict";
(() => {
  const API = "/assistant/api";
  const $ = (id) => document.getElementById(id);
  const els = {
    convs: $("convs"), convsEmpty: $("convs-empty"), thread: $("thread"), intro: $("intro"),
    form: $("composer"), input: $("input"), send: $("send"), newConv: $("new-conv"),
    who: $("who"), project: $("project"), sidebar: $("sidebar"), toggle: $("list-toggle"),
    tray: $("tray"), trayNote: $("tray-note"), fileInput: $("file-input"), attach: $("attach"), mic: $("mic"),
    drop: $("drop"), chat: $("chat"),
  };
  const MAX_BYTES = 50 * 1024 * 1024, MAX_FILES = 10;
  const state = { current: null, busy: false, source: null, live: null, pending: [], rec: null, youtrack: null, name: "Assistant" };
  const DRAFT_KINDS = [["bug", "Bug"], ["feature", "Feature"], ["research", "Research"]];
  const KIND = { image: "Image", audio: "Audio", video: "Video", pdf: "PDF", document: "Word", sheet: "Spreadsheet",
    slides: "Presentation", text: "Text", mail: "Email", archive: "Archive", other: "File" };

  async function api(path, opts = {}) {
    const res = await fetch(API + path, {
      ...opts, headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    });
    let body = {};
    try { body = await res.json(); } catch (_) { }
    if (!res.ok) throw new Error(body.error || `Error ${res.status}`);
    return body;
  }
  function md(text) {
    return DOMPurify.sanitize(marked.parse(text || "", { breaks: true, gfm: true }));
  }
  function when(ts) {
    const d = new Date(ts * 1000), now = new Date();
    const time = d.toLocaleTimeString("uk-UA", { hour: "2-digit", minute: "2-digit" });
    return d.toDateString() === now.toDateString() ? time
      : `${d.toLocaleDateString("uk-UA", { day: "numeric", month: "short" })}, ${time}`;
  }
  function scrollDown(force) {
    const t = els.thread;
    if (force || t.scrollHeight - t.scrollTop - t.clientHeight < 120) t.scrollTop = t.scrollHeight;
  }
  function setBusy(b) {
    state.busy = b;
    updateSend();
  }
  function updateSend() {
    const uploading = state.pending.some((p) => p.status === "uploading" || p.status === "recording");
    els.send.disabled = state.busy || uploading;
  }
  function size(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
    return `${(n / 1024 / 1024).toFixed(1)} MB`;
  }
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  const REACTION_CHARS = {"+1": "👍", white_check_mark: "✅", pray: "🙏", heart: "❤️", raised_hands: "🙌",
                          ok_hand: "👌", tada: "🎉", joy: "😂", muscle: "💪", fire: "🔥"};
  function messageEl(m) {
    const wrap = document.createElement("article");
    wrap.className = `msg ${m.role}`;
    if (m.id) wrap.dataset.id = m.id;
    const head = document.createElement("div");
    head.className = "head";
    const who = m.role === "user" ? (m.author || "You") : m.role === "assistant" ? state.name : "Error";
    head.textContent = `${who} · ${when(m.created_at)}`;
    const body = document.createElement("div");
    body.className = "body";
    if (m.role === "assistant" && m.meta && m.meta.silent) {
      wrap.classList.add("quiet");
      const mark = REACTION_CHARS[m.meta.reaction] || "";
      head.textContent = `${state.name} · ${when(m.created_at)}`;
      body.textContent = mark ? `Didn't reply in words, reacted with ${mark}` : "Didn't reply: the conversation wasn't for the assistant";
      wrap.append(head, body);
      return wrap;
    }
    if (m.role === "assistant") body.innerHTML = md(m.text);
    else body.textContent = m.text;
    wrap.append(head);
    if (m.attachments && m.attachments.length) wrap.append(attachmentsEl(m.attachments));
    if (m.text || m.role !== "user") wrap.append(body);
    for (const d of m.drafts || []) wrap.append(draftEl(d));
    for (const inv of m.investigations || []) wrap.append(inquiryEl(inv));
    if (m.role === "system" && m.id) {
      const retry = document.createElement("button");
      retry.type = "button"; retry.className = "btn retry"; retry.textContent = "Try again";
      retry.addEventListener("click", () => retryLast(retry));
      body.append(retry);
    }
    return wrap;
  }
  function attachmentsEl(list) {
    const box = el("div", "atts");
    const pics = el("div", "pics");
    for (const a of list) {
      if (a.kind === "image" && a.view_url) {
        const link = el("a", "pic");
        link.href = a.view_url; link.target = "_blank"; link.rel = "noopener";
        link.title = `${a.name}: open in full`;
        const img = el("img");
        img.src = a.view_url; img.alt = a.name; img.loading = "lazy";
        link.append(img);
        pics.append(link);
        continue;
      }
      if (a.kind === "audio" || a.kind === "video") {
        const media = el("figure", "media");
        const player = el(a.kind === "video" ? "video" : "audio");
        player.controls = true; player.preload = "metadata"; player.src = a.url;
        const cap = el("figcaption");
        cap.append(fileLink(a));
        const tr = el("p", "transcript");
        if (a.transcript) { tr.append(el("span", "label", "Transcript"), document.createTextNode(a.transcript)); }
        else if (a.status === "pending") { tr.classList.add("wait"); tr.textContent = "Transcript is being prepared…"; }
        else if (a.note) { tr.classList.add("wait"); tr.textContent = a.note; }
        media.append(player, cap);
        if (tr.textContent) media.append(tr);
        box.append(media);
        continue;
      }
      const f = el("div", "file");
      f.append(fileLink(a));
      if (a.kind === "image" && a.status === "pending") f.append(el("p", "note wait", "Preparing a preview…"));
      else if (a.note && a.status !== "pending") f.append(el("p", "note", a.note));
      box.append(f);
    }
    if (pics.childElementCount) box.prepend(pics);
    return box;
  }
  function launchEl(d) {
    const box = el("div", "draft-launch");
    box.append(el("p", "draft-launch-title", "For development to take the ticket"));
    const steps = el("ol", "draft-steps");
    const live = el("p", "draft-launch-live"); live.setAttribute("aria-live", "polite");
    const now = el("p", "draft-launch-now"); now.hidden = true;
    const link = el("a", null, `Open ${d.issue_key} in YouTrack`); link.href = d.issue_url; link.target = "_blank"; link.rel = "noopener";
    const refresh = el("button", "linkish", "Check again"); refresh.type = "button";
    const row = el("div", "draft-actions"); row.append(link, refresh);
    const step = (done, text) => { const li = el("li", done ? "done" : null); li.append(el("span", "mark", done ? "✓" : ""), el("span", null, text)); return li; };
    function draw(st) {
      steps.replaceChildren();
      now.hidden = !st;
      if (!st) {
        steps.append(step(false, "Move the ticket from Backlog to To Do."), step(false, "Put the agent tag on it."));
        live.hidden = true;
        return;
      }
      const state = st.state || "no state";
      const inQueue = st.state === st.queued_state;
      steps.append(step(st.taken || inQueue,
        st.taken ? `Moved to ${st.queued_state}.` : inQueue ? `In the ${st.queued_state} column.`
          : `Move the ticket to ${st.queued_state}.`));
      steps.append(step(st.tag_ok,
        st.tag_ok ? `${st.tag_by} put the ${st.tag} tag on it.`
          : st.tag_by ? `${st.tag_by} put the ${st.tag} tag on it, but development takes tickets only from: ${st.factory_users.join(", ")}. Ask one of them.`
          : `Put the ${st.tag} tag on the ticket.`));
      now.textContent = `Now in YouTrack: ${state}${st.assignee ? `, assignee ${st.assignee}` : ", no assignee"}.`;
      live.className = "draft-launch-live";
      if (st.taken) { live.textContent = "Development has taken the ticket."; live.classList.add("ok"); }
      else if (st.rejected) { live.textContent = "Development didn't take the ticket. The reason is in a comment in YouTrack."; live.classList.add("bad"); }
      else if (!inQueue && st.state && st.state !== "Backlog") live.textContent = `The ticket is in the "${state}" state, development hasn't taken it. If it is meant for development, move it back to ${st.queued_state}.`;
      else if (inQueue && st.tag_ok) live.textContent = "All set, development will take the ticket within a few minutes.";
      else live.textContent = "";
      live.hidden = !live.textContent;
    }
    let timer = null, tries = 0;
    async function check() {
      clearTimeout(timer);
      if (!box.isConnected && tries > 0) return;
      try { const st = await api(`/drafts/${d.id}/ticket`); draw(st); if (st.taken || st.rejected) return; }
      catch (e) { live.textContent = e.message; live.hidden = false; }
      if (++tries < 60) timer = setTimeout(check, 30000);
    }
    refresh.addEventListener("click", () => { tries = 1; check(); });
    draw(null);
    box.append(steps, now, live, row);
    requestAnimationFrame(check);
    return box;
  }
  function inquiryEl(inv) {
    const box = el("div", "inquiry");
    if (inv.state === "told") {
      box.classList.add("done");
      box.append(el("p", "inquiry-line", `Looked into it: "${inv.question}". The answer is below.`));
      return box;
    }
    box.dataset.pending = "1";
    const line = el("p", "inquiry-line"); line.setAttribute("role", "status");
    const say = () => {
      const since = inv.prep_started || inv.created_at;
      const s = Math.max(0, Math.round(Date.now() / 1000 - since));
      const took = s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`;
      line.textContent = `I'll look into it: "${inv.question}". I'll write here as soon as I find out, usually within a few minutes. ${took} so far.`;
    };
    say();
    const tick = setInterval(() => { if (!box.isConnected) { clearInterval(tick); return; } say(); }, 1000);
    box.append(line);
    return box;
  }
  setInterval(async () => {
    const id = state.current;
    if (!id || state.busy || !els.thread.querySelector(".inquiry[data-pending]")) return;
    let conv;
    try { conv = await api(`/conversations/${id}`); } catch (_) { return; }
    if (state.current !== id || state.busy) return;
    syncThread(id, conv);
  }, 8000);
  async function syncThread(id, conv) {
    if (!conv) { try { conv = await api(`/conversations/${id}`); } catch (_) { return; } }
    if (state.current !== id) return;
    const shown = new Map([...els.thread.querySelectorAll(".msg[data-id]")].map((n) => [n.dataset.id, n]));
    for (const m of conv.messages) {
      const node = shown.get(String(m.id));
      if (!node) {
        if (els.intro.isConnected) els.intro.remove();
        const live = state.live && state.live.wrap.isConnected ? state.live.wrap : null;
        live ? live.before(messageEl(m)) : els.thread.append(messageEl(m));
        continue;
      }
      const olds = [...node.querySelectorAll(":scope > .inquiry")];
      const news = (m.investigations || []).map(inquiryEl);
      olds.forEach((o, i) => { if (news[i]) o.replaceWith(news[i]); else o.remove(); });
      for (const extra of news.slice(olds.length)) node.append(extra);
    }
    pruneRetries();
    if (conv.busy && !state.source) { setBusy(true); follow(id); }
  }
  function preparingEl(d, card, project, kindName) {
    const top = el("div", "draft-top");
    if (d.target_key) top.append(editLabel(d));
    else top.append(el("span", "draft-label", `Ticket draft · ${project}`), el("span", "draft-hint", kindName));
    const title = el("p", "draft-title", d.summary);
    const line = el("p", "draft-prep");
    line.setAttribute("role", "status");
    const say = () => {
      if (!d.prep_started) { line.textContent = `Preparing the ${d.target_key ? "edit" : "ticket"}, starting now.`; return; }
      const s = Math.max(0, Math.round(Date.now() / 1000 - d.prep_started));
      const took = s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`;
      line.textContent = `Preparing the ${d.target_key ? "edit" : "ticket"} and checking the details against the code. Usually a couple of minutes, ${took} so far.`;
    };
    say();
    const later = el("button", "linkish", "Dismiss"); later.type = "button";
    const err = el("p", "draft-error"); err.setAttribute("role", "alert"); err.hidden = true;
    later.addEventListener("click", async () => {
      later.disabled = true;
      try { card.replaceWith(draftEl(await api(`/drafts/${d.id}/dismiss`, { method: "POST", body: "{}" }))); }
      catch (e) { later.disabled = false; err.textContent = e.message; err.hidden = false; }
    });
    const actions = el("div", "draft-actions"); actions.append(later);
    card.append(top, title, line, err, actions);
    let polls = 0;
    const tick = setInterval(say, 1000);
    const poll = setInterval(async () => {
      if (!card.isConnected) { clearInterval(poll); clearInterval(tick); return; }
      if (++polls % 5) return;
      try {
        const fresh = await api(`/drafts/${d.id}`);
        if (fresh.state !== "preparing") { clearInterval(poll); clearInterval(tick); card.replaceWith(draftEl(fresh)); return; }
        Object.assign(d, fresh);
      } catch (_) { }
    }, 1000);
    return card;
  }
  function editLabel(d) {
    const box = el("span", "draft-label");
    const yt = state.youtrack || {};
    const link = el("a", null, d.target_key); link.href = `${yt.url || ""}/issue/${d.target_key}`; link.target = "_blank"; link.rel = "noopener";
    box.append("Ticket edit · ", link);
    return box;
  }
  function beforeEl(d) {
    const more = el("details", "draft-text");
    more.append(el("summary", null, `Now in ${d.target_key}: "${d.target_summary || ""}"`));
    const before = (d.target_before || "").replace(/\n*^-{3,}[ \t]*\n(?:Created|Updated) by the factory assistant[^\n]*\n?/gm, "\n\n")
      .replace(/\n{3,}/g, "\n\n").trim();
    const text = el("div", "draft-md"); text.innerHTML = md(before);
    more.append(text);
    return more;
  }
  function checkEl(d) {
    if (d.checked === "passed") return el("p", "draft-check ok", "Checked against the code, everything matches.");
    if (d.checked === "notes") {
      const box = el("div", "draft-check notes");
      box.append(el("p", "draft-check-title", d.target_key ? "Worth a look before updating" : "Worth a look before creating"));
      const body = el("div", "draft-md"); body.innerHTML = md(d.review_notes || "");
      box.append(body, el("p", "draft-hint", d.target_key ? "You can fix the text above or update it as is, it's your call."
        : "You can fix the text above or create it as is, it's your call."));
      return box;
    }
    if (d.checked === "stale") {
      const box = el("div", "draft-check unchecked");
      box.append(el("p", "draft-check-title", "You changed the text after it was checked against the code; the new version hasn't been checked."));
      if (d.review_notes) {
        box.append(el("p", "draft-hint", "What was worth a look in the previous version:"));
        const body = el("div", "draft-md"); body.innerHTML = md(d.review_notes); box.append(body);
      }
      return box;
    }
    if (d.checked === "unchecked") return el("p", "draft-check unchecked",
      `Couldn't check against the code${d.review_notes ? `: ${d.review_notes.replace(/\.$/, "")}` : ""}. Review the text more carefully.`);
    return null;
  }
  function draftEl(d) {
    const card = el("section", `draft ${d.state}`);
    card.dataset.draft = d.id;
    const yt = state.youtrack || {};
    const project = yt.project || "YouTrack";
    const kindName = (DRAFT_KINDS.find(([k]) => k === d.kind) || [null, "Ticket"])[1];
    if (d.state === "preparing") return preparingEl(d, card, project, kindName);
    if (d.state === "created" || d.state === "dismissed") {
      const head = el("div", "draft-done");
      if (d.state === "created") {
        const link = el("a", "draft-key", d.issue_key);
        link.href = d.issue_url; link.target = "_blank"; link.rel = "noopener";
        head.append(el("span", "draft-ok", d.target_key ? "Ticket updated" : "Ticket created"), link, el("span", "draft-title", d.summary));
      } else {
        head.append(el("span", "draft-label", d.target_key ? `Edit to ${d.target_key} dismissed` : "Draft dismissed"),
          el("span", "draft-title", d.summary));
      }
      const meta = el("p", "draft-meta", `${d.target_key ? "Edit" : kindName} · ${d.acted_by || ""} · ${d.acted_at ? when(d.acted_at) : ""}` +
        (d.board ? ` · ${d.board}` : "") +
        (d.tagged ? ` · ${d.tagged.startsWith("tag ") ? d.tagged : `tag ${d.tagged}: development will take it on`}` : ""));
      const more = el("details", "draft-text");
      more.append(el("summary", null, "Ticket text"));
      const text = el("div", "draft-md"); text.innerHTML = md(d.description);
      more.append(text);
      card.append(head, meta);
      if (d.state === "created" && !yt.queue_tag && !d.target_key) card.append(launchEl(d));
      card.append(more);
      return card;
    }
    const top = el("div", "draft-top");
    const kind = el("select", "draft-kind");
    kind.setAttribute("aria-label", "Ticket type");
    for (const [k, label] of DRAFT_KINDS) { const o = el("option", null, label); o.value = k; o.selected = k === d.kind; kind.append(o); }
    if (d.target_key) top.append(editLabel(d));
    else top.append(el("span", "draft-label", `Ticket draft · ${project}`), kind);
    const sumId = `draft-${d.id}-summary`, descId = `draft-${d.id}-description`;
    const sumLabel = el("label", "sr", "Ticket title"); sumLabel.htmlFor = sumId;
    const summary = el("input", "draft-summary"); summary.id = sumId; summary.value = d.summary; summary.maxLength = 200;
    const descLabel = el("label", "sr", "Ticket description"); descLabel.htmlFor = descId;
    const desc = el("textarea", "draft-desc"); desc.id = descId; desc.value = d.description; desc.rows = 8;
    const err = el("p", "draft-error"); err.setAttribute("role", "alert");
    const showErr = (t) => { err.textContent = t || ""; err.hidden = !t; };
    showErr(d.error || (yt.ready === false ? `YouTrack isn't connected yet: the assistant has no bot token. The draft is saved; the ticket can be created as soon as the token is there.` : ""));
    const actions = el("div", "draft-actions");
    const verb = d.target_key ? "Update ticket" : "Create ticket", busyVerb = d.target_key ? "Updating…" : "Creating…";
    const create = el("button", "btn primary", verb); create.type = "button";
    const later = el("button", "linkish", "Dismiss"); later.type = "button";
    const hint = el("span", "draft-hint", d.target_key ? `replaces the title and description of ${d.target_key}; the previous text stays in the YouTrack history`
      : yt.queue_tag ? `with the ${yt.queue_tag} tag: development will take it on right away` : "the ticket will appear in Backlog on the board; you start it yourself");
    actions.append(create, later, hint);
    if (d.state === "creating") { create.disabled = true; create.textContent = busyVerb; }
    const values = () => ({ kind: kind.value, summary: summary.value.trim(), description: desc.value.trim() });
    const lock = (b) => { [create, later, kind, summary, desc].forEach((x) => { x.disabled = b; }); };
    async function save() {
      const v = values();
      if (!v.summary || !v.description) throw new Error("A ticket needs a title and a description.");
      if (v.kind === d.kind && v.summary === d.summary && v.description === d.description) return;
      const fresh = await api(`/drafts/${d.id}`, { method: "PATCH", body: JSON.stringify(v) });
      const was = d.checked;
      Object.assign(d, fresh);
      if (was !== d.checked) {
        const now = checkEl(d), old = card.querySelector(".draft-check");
        if (old && now) old.replaceWith(now); else if (old) old.remove(); else if (now) err.before(now);
      }
    }
    create.addEventListener("click", async () => {
      lock(true); create.textContent = busyVerb; showErr("");
      try {
        await save();
        const res = await fetch(`${API}/drafts/${d.id}/create`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
        let body = {}; try { body = await res.json(); } catch (_) { }
        if (res.ok) { card.replaceWith(draftEl(body)); return; }
        Object.assign(d, body);
        throw new Error(body.error || `Failed (error ${res.status}).`);
      } catch (e) { lock(false); create.textContent = verb; showErr(e.message); }
    });
    later.addEventListener("click", async () => {
      lock(true);
      try { card.replaceWith(draftEl(await api(`/drafts/${d.id}/dismiss`, { method: "POST", body: "{}" }))); }
      catch (e) { lock(false); showErr(e.message); }
    });
    for (const f of [kind, summary, desc]) f.addEventListener("change", () => save().then(() => showErr(d.error || "")).catch((e) => showErr(e.message)));
    const growDesc = () => { desc.style.height = "auto"; desc.style.height = `${Math.min(desc.scrollHeight + 2, window.innerHeight * 0.5)}px`; };
    desc.addEventListener("input", growDesc);
    requestAnimationFrame(growDesc);
    card.append(top, sumLabel, summary, descLabel, desc);
    if (d.target_key) card.append(beforeEl(d));
    const check = checkEl(d);
    if (check) card.append(check);
    card.append(err, actions);
    return card;
  }

  function fileLink(a) {
    const link = el("a", "file-link");
    link.href = `${a.url}?download`; link.download = a.name;
    link.append(el("span", "kind", KIND[a.kind] || "File"), el("span", "name", a.name), el("span", "size", size(a.size)));
    return link;
  }

  function pruneRetries() {
    const all = [...els.thread.querySelectorAll(".retry")];
    const last = els.thread.lastElementChild;
    all.forEach((b) => { if (!last || !last.contains(b)) b.remove(); });
  }
  async function retryLast(btn) {
    const id = state.current;
    btn.disabled = true;
    try {
      await api(`/conversations/${id}/retry`, { method: "POST", body: "{}" });
      btn.remove();
      setBusy(true);
      follow(id);
    } catch (e) { btn.disabled = false; showError(e.message); }
  }
  function liveEl() {
    const wrap = document.createElement("article");
    wrap.className = "msg assistant live";
    const head = document.createElement("div");
    head.className = "head";
    head.textContent = `${state.name} · writing…`;
    const body = document.createElement("div");
    body.className = "body";
    const status = document.createElement("p");
    status.className = "status";
    status.textContent = "Thinking…";
    body.append(status);
    wrap.append(head, body);
    return { wrap, body, status, text: "" };
  }
  function renderLive(live) {
    const keep = live.status;
    live.body.innerHTML = live.text ? md(live.text) : "";
    if (keep.textContent) live.body.append(keep);
  }

  async function loadList() {
    const { conversations } = await api("/conversations");
    els.convs.replaceChildren(...conversations.map((c) => {
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = `#${c.id}`;
      if (state.current === c.id) a.setAttribute("aria-current", "page");
      const t = document.createElement("span"); t.className = "t"; t.textContent = c.title;
      const m = document.createElement("span"); m.className = "m";
      m.textContent = `${c.created_by.split("@")[0]} · ${when(c.updated_at)}`;
      if (c.busy) { const b = document.createElement("span"); b.className = "busy"; b.textContent = " · answering"; m.append(b); }
      a.append(t, m);
      li.append(a);
      return li;
    }));
    els.convsEmpty.hidden = conversations.length > 0;
    return conversations;
  }

  async function open(id) {
    if (state.source) { state.source.close(); state.source = null; }
    clearTimeout(state.retryTimer); state.retryNow = null;
    if (id !== state.current) clearPending(true);
    state.live = null;
    state.current = id;
    closeSidebar();
    if (!id) {
      els.thread.replaceChildren(els.intro);
      setBusy(false);
      return;
    }
    let conv;
    try { conv = await api(`/conversations/${id}`); }
    catch (e) { if (state.current === id) showError(e.message); return; }
    if (state.current !== id) return;
    els.thread.replaceChildren(...conv.messages.map(messageEl));
    if (!conv.messages.length) els.thread.replaceChildren(els.intro);
    pruneRetries();
    document.title = `${conv.title} · ${state.name}`;
    setBusy(conv.busy);
    if (conv.busy) follow(id);
    scrollDown(true);
    loadList();
  }

  async function refreshAttachments(id) {
    const stale = [...els.thread.querySelectorAll(".msg.user .wait")];
    if (!stale.length) return;
    let conv;
    try { conv = await api(`/conversations/${id}`); } catch (_) { return; }
    if (state.current !== id) return;
    const byId = new Map(conv.messages.map((m) => [String(m.id), m]));
    for (const node of els.thread.querySelectorAll(".msg.user[data-id]")) {
      if (!node.querySelector(".wait")) continue;
      const m = byId.get(node.dataset.id);
      if (m) node.replaceWith(messageEl(m));
    }
  }

  function showError(text) {
    els.thread.append(messageEl({ role: "system", text, created_at: Date.now() / 1000 }));
    scrollDown(true);
  }

  function follow(id) {
    const live = liveEl();
    state.live = live;
    els.thread.append(live.wrap);
    scrollDown(true);
    const src = new EventSource(`${API}/conversations/${id}/events`);
    state.source = src;
    let ended = false;
    src.onmessage = (e) => {
      const ev = JSON.parse(e.data);
      if (state.current !== id) { src.close(); return; }
      if (ev.type === "snapshot") { live.text = ev.text; if (ev.status) live.status.textContent = ev.status; renderLive(live); }
      else if (ev.type === "delta") { live.text += ev.text; live.status.textContent = live.text ? "" : live.status.textContent; renderLive(live); }
      else if (ev.type === "status") { live.status.textContent = ev.text; renderLive(live); }
      else if (ev.type === "done" || ev.type === "error") {
        ended = true;
        src.close(); state.source = null;
        live.wrap.replaceWith(messageEl(ev.message));
        pruneRetries();
        setBusy(false); loadList();
        refreshAttachments(id);
        if (els.thread.querySelector(".inquiry[data-pending]")) syncThread(id);
      } else if (ev.type === "idle") {
        ended = true; src.close(); state.source = null;
        settle(id);
        return;
      }
      scrollDown(false);
    };
    src.onerror = () => {
      if (ended) return;
      src.close(); state.source = null;
      live.status.textContent = "Connection lost, reconnecting…";
      renderLive(live);
      reconnect(id, 0);
    };
  }

  function reconnect(id, attempt) {
    clearTimeout(state.retryTimer);
    const delay = Math.min(2000 * 2 ** attempt, 15000);
    state.retryTimer = setTimeout(() => { if (state.current === id) settle(id, attempt); }, delay);
    state.retryNow = () => { if (state.current === id) { clearTimeout(state.retryTimer); settle(id, attempt); } };
  }
  async function settle(id, attempt = 0) {
    let conv;
    try { conv = await api(`/conversations/${id}`); }
    catch (_) { if (state.current === id) reconnect(id, attempt + 1); return; }
    if (state.current !== id) return;
    state.retryNow = null;
    if (state.live && state.live.wrap.isConnected) state.live.wrap.remove();
    state.live = null;
    setBusy(false);
    await syncThread(id, conv);
    loadList();
    refreshAttachments(id);
    scrollDown(false);
  }
  window.addEventListener("online", () => { if (state.retryNow) { const f = state.retryNow; state.retryNow = null; f(); } });

  function ensureConversation() {
    if (state.current) return Promise.resolve(state.current);
    if (!state.creating) {
      state.creating = api("/conversations", { method: "POST", body: "{}" }).then((conv) => {
        state.current = conv.id;
        history.replaceState(null, "", `#${conv.id}`);
        return conv.id;
      }).finally(() => { state.creating = null; });
    }
    return state.creating;
  }

  async function send(text) {
    const ready = state.pending.filter((p) => p.status === "ready");
    const ids = ready.map((p) => p.att.id);
    setBusy(true);
    let id;
    try {
      id = await ensureConversation();
      const { message } = await api(`/conversations/${id}/messages`, { method: "POST", body: JSON.stringify({ text, attachments: ids }) });
      state.pending = state.pending.filter((p) => !ready.includes(p) && p.status !== "error");
      renderTray();
      if (state.current !== id) { loadList(); return; }
      if (els.intro.isConnected) els.thread.replaceChildren();
      els.thread.append(messageEl(message));
      follow(id);
      loadList();
    } catch (e) {
      if (id && state.current !== id) return;
      setBusy(false);
      showError(e.message);
      if (!els.input.value) { els.input.value = text; grow(); }
    }
  }

  els.form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = els.input.value.trim();
    const files = state.pending.some((p) => p.status === "ready");
    if ((!text && !files) || els.send.disabled) return;
    els.input.value = "";
    grow();
    send(text);
  });

  function note(text) {
    els.trayNote.textContent = text || "";
    els.trayNote.hidden = !text;
  }
  function renderTray() {
    els.tray.replaceChildren(...state.pending.map((p) => {
      const li = el("li", `chip ${p.status}`);
      const name = el("span", "name", p.name);
      const meta = el("span", "meta");
      if (p.status === "uploading") meta.textContent = `${size(p.size)} · ${p.progress || 0}%`;
      else if (p.status === "recording") meta.textContent = `recording ${clock(p.seconds || 0)}`;
      else if (p.status === "error") meta.textContent = p.error;
      else meta.textContent = size(p.size);
      const bar = el("span", "bar");
      bar.style.setProperty("--p", `${p.status === "uploading" ? p.progress || 0 : 100}%`);
      const x = el("button", "remove");
      x.type = "button";
      x.setAttribute("aria-label", p.status === "recording" ? "Cancel recording" : `Remove ${p.name}`);
      x.textContent = "×";
      x.addEventListener("click", () => removePending(p));
      li.append(name, meta, bar, x);
      return li;
    }));
    els.tray.hidden = state.pending.length === 0;
    updateSend();
  }
  function clock(sec) { return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`; }

  function removePending(p) {
    if (p.status === "recording") { stopRecording(true); return; }
    if (p.xhr) p.xhr.abort();
    if (p.att) fetch(`${API}/files/${p.att.id}`, { method: "DELETE" }).catch(() => {});
    state.pending = state.pending.filter((x) => x !== p);
    note("");
    renderTray();
  }
  function clearPending(discard) {
    if (state.rec) stopRecording(true);
    if (discard) state.pending.forEach((p) => { if (p.xhr) p.xhr.abort(); if (p.att) fetch(`${API}/files/${p.att.id}`, { method: "DELETE" }).catch(() => {}); });
    state.pending = [];
    note("");
    renderTray();
  }

  function addFiles(list) {
    const files = [...list];
    if (!files.length) return;
    note("");
    const room = MAX_FILES - state.pending.filter((p) => p.status !== "error").length;
    if (files.length > room) {
      note(`One message can have no more than ${MAX_FILES} files. ${room > 0 ? `Added the first ${room}.` : "Send these, and the rest in the next message."}`);
    }
    files.slice(0, Math.max(room, 0)).forEach((f) => upload(f));
  }

  async function upload(file, name) {
    const p = { key: Math.random().toString(36).slice(2), name: name || file.name || "file", size: file.size, status: "uploading", progress: 0 };
    state.pending.push(p);
    if (file.size > MAX_BYTES) { p.status = "error"; p.error = "over 50 MB, can't be sent"; renderTray(); return; }
    if (!file.size) { p.status = "error"; p.error = "empty file"; renderTray(); return; }
    renderTray();
    let cid;
    try { cid = await ensureConversation(); }
    catch (e) { p.status = "error"; p.error = e.message; renderTray(); return; }
    const xhr = new XMLHttpRequest();
    p.xhr = xhr;
    xhr.open("POST", `${API}/conversations/${cid}/files?name=${encodeURIComponent(p.name)}`);
    xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) { p.progress = Math.round((e.loaded / e.total) * 100); renderTray(); } };
    xhr.onload = () => {
      p.xhr = null;
      let body = {};
      try { body = JSON.parse(xhr.responseText); } catch (_) { }
      if (xhr.status === 201) { p.status = "ready"; p.att = body; }
      else { p.status = "error"; p.error = body.error || `didn't upload (error ${xhr.status})`; }
      if (!state.pending.includes(p) && p.att) fetch(`${API}/files/${p.att.id}`, { method: "DELETE" }).catch(() => {});
      renderTray();
    };
    xhr.onerror = () => { p.xhr = null; p.status = "error"; p.error = "connection lost, add the file again"; renderTray(); };
    xhr.onabort = () => { p.xhr = null; };
    xhr.send(file);
  }

  els.attach.addEventListener("click", () => els.fileInput.click());
  els.fileInput.addEventListener("change", () => { addFiles(els.fileInput.files); els.fileInput.value = ""; });
  els.input.addEventListener("paste", (e) => {
    const files = [...(e.clipboardData?.files || [])];
    if (!files.length) return;
    e.preventDefault();
    const stamp = new Date().toLocaleTimeString("uk-UA", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).replace(/:/g, "-");
    files.forEach((f, i) => {
      const ext = (f.type.split("/")[1] || "png").replace("jpeg", "jpg");
      const named = f.name && f.name !== "image.png" ? f.name : `Screenshot ${stamp}${files.length > 1 ? `-${i + 1}` : ""}.${ext}`;
      if (state.pending.filter((p) => p.status !== "error").length >= MAX_FILES) { note(`No more than ${MAX_FILES} files in one message.`); return; }
      upload(f, named);
    });
  });
  let dragDepth = 0;
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
  els.chat.addEventListener("dragenter", (e) => { if (!hasFiles(e)) return; e.preventDefault(); dragDepth++; els.chat.classList.add("dragging"); });
  els.chat.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
  els.chat.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; els.chat.classList.remove("dragging"); } });
  els.chat.addEventListener("drop", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault(); dragDepth = 0; els.chat.classList.remove("dragging");
    addFiles(e.dataTransfer.files);
  });

  async function startRecording() {
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      note("This browser can't record sound. Record the voice message in another app and add it as a file.");
      return;
    }
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
    catch (_) { note("The browser didn't give access to the microphone. Allow it in the site settings and try again."); return; }
    const type = ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus"].find((t) => MediaRecorder.isTypeSupported(t)) || "";
    const rec = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
    const chunks = [];
    const p = { key: "rec", name: "Voice message", size: 0, status: "recording", seconds: 0 };
    state.rec = { rec, stream, p, cancel: false, started: Date.now() };
    state.pending.push(p);
    note("");
    rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
    rec.onstop = () => {
      stream.getTracks().forEach((t) => t.stop());
      clearInterval(state.rec?.timer);
      const cancelled = state.rec?.cancel;
      state.rec = null;
      state.pending = state.pending.filter((x) => x !== p);
      els.mic.setAttribute("aria-pressed", "false");
      els.mic.setAttribute("aria-label", "Record a voice message"); els.mic.title = "Record a voice message";
      renderTray();
      if (cancelled || !chunks.length) return;
      const mime = rec.mimeType || "audio/webm";
      const ext = mime.includes("mp4") ? "m4a" : mime.includes("ogg") ? "ogg" : "webm";
      const t = new Date().toLocaleTimeString("uk-UA", { hour: "2-digit", minute: "2-digit" }).replace(":", "-");
      upload(new File(chunks, `Voice message ${t}.${ext}`, { type: mime.split(";")[0] }));
    };
    state.rec.timer = setInterval(() => { p.seconds = Math.round((Date.now() - state.rec.started) / 1000); renderTray(); }, 500);
    rec.start(1000);
    els.mic.setAttribute("aria-pressed", "true");
    els.mic.setAttribute("aria-label", "Stop recording"); els.mic.title = "Stop recording";
    renderTray();
  }
  function stopRecording(cancel) {
    if (!state.rec) return;
    state.rec.cancel = !!cancel;
    if (state.rec.rec.state !== "inactive") state.rec.rec.stop();
  }
  els.mic.addEventListener("click", () => { if (state.rec) stopRecording(false); else startRecording(); });
  els.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); els.form.requestSubmit(); }
  });
  const narrow = window.matchMedia("(max-width: 760px)");
  const hint = () => { els.input.placeholder = narrow.matches ? "Message…" : "Write a message…"; };
  narrow.addEventListener("change", hint);
  hint();
  function grow() { els.input.style.height = "auto"; els.input.style.height = `${els.input.scrollHeight + 2}px`; }
  els.input.addEventListener("input", grow);
  els.newConv.addEventListener("click", () => { history.pushState(null, "", "#"); open(null); els.input.focus(); });
  window.addEventListener("hashchange", () => open(location.hash.slice(1) || null));

  function closeSidebar() { els.sidebar.classList.remove("open"); els.toggle.setAttribute("aria-expanded", "false"); }
  els.toggle.addEventListener("click", () => {
    const open_ = !els.sidebar.classList.contains("open");
    els.sidebar.classList.toggle("open", open_);
    els.toggle.setAttribute("aria-expanded", String(open_));
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSidebar(); });

  (async () => {
    try {
      const me = await api("/me");
      els.who.textContent = me.user;
      state.youtrack = me.youtrack || null;
      if (me.name) state.name = me.name.split(" ")[0];
      if (me.project) els.project.textContent = `${state.name} · ${me.project}`;
      if (me.name && me.name !== "Assistant") {
        document.querySelector("#intro h1").textContent = me.name;
        if (!location.hash.slice(1)) document.title = me.name;
      }
      if (me.intro) document.querySelector("#intro .lede").textContent = me.intro;
    } catch (e) { showError(e.message); return; }
    await loadList();
    await open(location.hash.slice(1) || null);
    updateSend();
  })();
})();
