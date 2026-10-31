/* ProjectMind dashboard.
 *
 * Plain JavaScript, no build step, nothing loaded from anywhere else. Every value
 * that comes from the daemon is put on the page as text (textContent), never as
 * markup, because it is the user's own notes and prompts.
 */
(() => {
  "use strict";

  // --- token ---------------------------------------------------------------
  // The token arrives in the URL fragment, which the browser never sends to a
  // server. It is moved to session storage and removed from the address bar.
  const TOKEN_KEY = "projectmind-token";
  const fromHash = /(?:^#|&)token=([^&]+)/.exec(location.hash);
  if (fromHash) {
    try { sessionStorage.setItem(TOKEN_KEY, decodeURIComponent(fromHash[1])); } catch (_) { /* private mode */ }
    // `&go=` names the view to land on, e.g. go=%2Fask%3Fq%3D... for a shared question.
    const go = /&go=([^&]+)/.exec(location.hash);
    history.replaceState(null, "", location.pathname + "#" + (go ? decodeURIComponent(go[1]) : "/ask"));
  }
  let token = "";
  try { token = sessionStorage.getItem(TOKEN_KEY) || ""; } catch (_) { token = ""; }

  // --- tiny DOM helper -------------------------------------------------------
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") el.className = value;
      else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
      // Through the CSSOM: the page's CSP forbids style attributes, not this.
      else if (key === "style") el.style.cssText = value;
      else el.setAttribute(key, value === true ? "" : String(value));
    }
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }
  const $ = (id) => document.getElementById(id);
  const fmt = new Intl.NumberFormat();
  const num = (n) => fmt.format(n || 0);
  const pct = (x) => (x === null || x === undefined ? "–" : `${(x * 100).toFixed(1)}%`);
  const ms = (x) => (x === null || x === undefined ? "–" : `${Math.round(x)} ms`);
  function when(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    const diff = (Date.now() - d.getTime()) / 1000;
    if (diff < 60) return "just now";
    if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} h ago`;
    return d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  }
  function toast(text) {
    const el = $("toast");
    el.textContent = text;
    el.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { el.hidden = true; }, 2200);
  }
  function copyButton(text) {
    return h("button", {
      class: "copy", type: "button", title: "Copy",
      onclick: async () => {
        try { await navigator.clipboard.writeText(text); toast("Copied"); } catch (_) { toast("Could not copy"); }
      },
    }, "copy");
  }

  // --- api -----------------------------------------------------------------
  async function api(method, path, body) {
    const response = await fetch(`/api/${path}`, {
      method,
      headers: { Authorization: `Bearer ${token}`, ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
      cache: "no-store",
    });
    if (response.status === 401) { showGate("The access token was not accepted."); throw new Error("unauthorized"); }
    const data = response.status === 204 ? {} : await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `request failed (${response.status})`);
    return data;
  }

  // --- icons ---------------------------------------------------------------
  const ICONS = {
    ask: "M12 3a7 7 0 0 1 7 7c0 2.4-1.2 4.2-2.9 5.4-.7.5-1.1 1.2-1.1 2V18H9v-.6c0-.8-.4-1.5-1.1-2C6.2 14.2 5 12.4 5 10a7 7 0 0 1 7-7Zm-3 18h6",
    search: "M11 4a7 7 0 1 1 0 14 7 7 0 0 1 0-14Zm9 16-4-4",
    history: "M4 12a8 8 0 1 0 2.3-5.7M4 4v4h4M12 8v4l3 2",
    knowledge: "M4 5c0-1 3.6-2 8-2s8 1 8 2-3.6 2-8 2-8-1-8-2Zm0 0v14c0 1 3.6 2 8 2s8-1 8-2V5M4 12c0 1 3.6 2 8 2s8-1 8-2",
    profile: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm-7 8c.8-3.4 3.6-5 7-5s6.2 1.6 7 5",
    eval: "M5 19V9m7 10V5m7 14v-7",
    tour: "M5 4l14 8-14 8V4Z",
    system: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm7.4-3a7.4 7.4 0 0 0-.1-1.2l2-1.6-2-3.4-2.4 1a7 7 0 0 0-2-1.2L14.5 3h-4l-.4 2.6a7 7 0 0 0-2 1.2l-2.4-1-2 3.4 2 1.6a7.4 7.4 0 0 0 0 2.4l-2 1.6 2 3.4 2.4-1a7 7 0 0 0 2 1.2l.4 2.6h4l.4-2.6a7 7 0 0 0 2-1.2l2.4 1 2-3.4-2-1.6c.1-.4.1-.8.1-1.2Z",
  };
  function icon(name) {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.8");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", ICONS[name] || ICONS.ask);
    svg.append(path);
    return svg;
  }

  // --- state and routing -------------------------------------------------------
  const state = { overview: null, demo: false, lastAsk: null, history: { served: "", source: "" }, profileStatus: "proposed" };

  const VIEWS = {
    ask: { title: "Ask", sub: "What would your agent be told before it answers this?", render: renderAsk },
    search: { title: "Search", sub: "Look through everything indexed, by words and by meaning.", render: renderSearch },
    history: { title: "History", sub: "Every question asked, what was served, and why.", render: renderHistory },
    knowledge: { title: "Knowledge", sub: "What has been read, and from where.", render: renderKnowledge },
    profile: { title: "Profile", sub: "How you work, as statements you approve. Only approved ones are ever served.", render: renderProfile },
    eval: { title: "Evaluation", sub: "Measured on questions about your own material.", render: renderEval },
    tour: { title: "Tour", sub: "Seven scripted questions through the real front door, each checked.", render: renderTour },
    system: { title: "System", sub: "Daemon, settings and storage.", render: renderSystem },
  };
  function navOrder() {
    return state.demo
      ? ["ask", "search", "tour", "history", "knowledge", "profile", "system"]
      : ["ask", "search", "history", "knowledge", "profile", "eval", "system"];
  }
  function currentView() {
    const name = (location.hash.match(/^#\/(\w+)/) || [])[1];
    return VIEWS[name] ? name : "ask";
  }
  function renderNav() {
    const nav = $("nav");
    nav.replaceChildren(...navOrder().map((name) =>
      h("a", { class: `nav-item${currentView() === name ? " active" : ""}`, href: `#/${name}` }, icon(name), VIEWS[name].title)));
  }
  async function route() {
    const name = currentView();
    const view = VIEWS[name];
    $("view-title").textContent = view.title;
    $("view-sub").textContent = view.sub;
    renderNav();
    const root = $("view");
    root.replaceChildren(h("div", { class: "skeleton" }));
    try {
      await view.render(root);
    } catch (error) {
      if (String(error.message) !== "unauthorized") {
        root.replaceChildren(h("div", { class: "card empty" }, h("div", { class: "big" }, "Something went wrong"), String(error.message)));
      }
    }
  }
  window.addEventListener("hashchange", route);

  // --- frame: overview and daemon status ----------------------------------------
  async function loadOverview() {
    const data = await api("GET", "overview");
    state.overview = data;
    state.demo = Boolean(data.demo);
    $("version").textContent = `v${data.version}`;
    $("mode-badge").hidden = !state.demo;
    const syncing = data.daemon && data.daemon.syncing;
    $("daemon-dot").className = `dot ${syncing ? "busy" : "ok"}`;
    $("daemon-text").textContent = syncing ? "syncing" : "daemon running";
    $("topbar-stats").replaceChildren(
      h("span", {}, h("b", {}, num(data.documents)), " documents"),
      h("span", {}, h("b", {}, num(data.passages)), " passages"),
      h("span", {}, h("b", {}, num(data.projects)), " projects"),
    );
    return data;
  }

  // --- ask -------------------------------------------------------------------
  async function renderAsk(root) {
    const prompt = h("textarea", { class: "input", id: "ask-prompt", placeholder: "Ask what you would ask your coding agent…", rows: 3 });
    const project = h("input", { class: "input", id: "ask-project", placeholder: "Project folder (optional)", spellcheck: "false" });
    const button = h("button", { class: "btn primary", type: "submit" }, "Ask");
    const result = h("div", { class: "stack" });
    const chips = h("div", { class: "chips" });

    async function submit(event) {
      if (event) event.preventDefault();
      const text = prompt.value.trim();
      if (!text) { prompt.focus(); return; }
      button.disabled = true;
      button.replaceChildren(h("span", { class: "spinner" }), "Asking");
      try {
        const data = await api("POST", "ask", { prompt: text, project: project.value.trim() });
        state.lastAsk = { prompt: text, project: project.value.trim(), data };
        result.replaceChildren(askResult(data));
      } catch (error) {
        if (error.message !== "unauthorized") result.replaceChildren(h("div", { class: "card empty" }, String(error.message)));
      } finally {
        button.disabled = false;
        button.replaceChildren("Ask");
      }
    }
    prompt.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) submit(event);
    });

    const form = h("form", { class: "card ask-card", onsubmit: submit },
      prompt,
      h("div", { class: "ask-row" },
        h("div", { class: "small muted" }, "The same front door the hook uses. ", h("span", { class: "kbd" }, "Ctrl"), " + ", h("span", { class: "kbd" }, "Enter"), " to ask."),
        project, button),
    );
    root.replaceChildren(form, chips, result);

    if (state.lastAsk) {
      prompt.value = state.lastAsk.prompt;
      project.value = state.lastAsk.project;
      result.replaceChildren(askResult(state.lastAsk.data));
    }
    let items = [];
    try {
      ({ items } = await api("GET", "suggestions"));
      chips.replaceChildren(...items.map((item) => h("button", {
        class: "chip", type: "button", title: item.project || "",
        onclick: () => { prompt.value = item.prompt; project.value = item.project || ""; submit(); },
      }, item.label ? h("span", { class: "chip-label" }, item.label) : null, item.prompt)));
    } catch (_) { /* suggestions are optional */ }
    // A link may carry a question: #/ask?q=...&p=... (p is a folder), or s=<n> for the
    // n-th suggestion, which is how a demo opens on an answer.
    const query = new URLSearchParams((location.hash.split("?")[1]) || "");
    const pick = items[Number(query.get("s"))];
    if (!state.lastAsk && (query.get("q") || pick)) {
      prompt.value = pick ? pick.prompt : query.get("q");
      project.value = pick ? pick.project || "" : query.get("p") || "";
      submit();
    } else if (!state.lastAsk) prompt.focus();
  }

  function verdictOf(data) {
    const served = data.evidence.length + data.records.length + data.profile.length;
    if (served) return { cls: "good", mark: "✓", text: `Served ${served} item${served === 1 ? "" : "s"}` };
    if (/trivial|mechanical/i.test(data.reason)) return { cls: "muted", mark: "–", text: "Skipped: nothing to look up" };
    return { cls: "warn", mark: "∅", text: "Silent: nothing covers this question" };
  }

  function askResult(data) {
    const verdict = verdictOf(data);
    const t = data.tokens;
    const cap = t.cap || 2300;
    const width = (n) => `${Math.max(0, Math.min(100, (n / cap) * 100))}%`;
    const summary = h("div", { class: "card" },
      h("div", { class: "verdict" },
        h("div", { class: `verdict-icon ${verdict.cls}` }, verdict.mark),
        h("div", {},
          h("div", { class: "headline" }, verdict.text),
          h("div", { class: "muted" }, data.reason || ""))),
      h("div", { class: "metrics" },
        metric("Task", data.task_type || "–"),
        metric("Project", data.project || "none"),
        metric("Latency", ms(data.latency_ms)),
        h("div", { class: "metric" },
          h("div", { class: "k" }, "Tokens"),
          h("div", { class: "v" }, `${num(t.total)} `, h("span", { class: "muted small" }, `of ${num(cap)}`)),
          h("div", { class: "budget", title: `evidence ${t.evidence} · records ${t.records} · profile ${t.profile}` },
            h("span", { style: `width:${width(t.evidence)};background:var(--accent)` }),
            h("span", { style: `width:${width(t.records)};background:var(--accent-2)` }),
            h("span", { style: `width:${width(t.profile)};background:var(--warn)` }))),
        data.entities && data.entities.length ? metric("Entities", data.entities.join(", ")) : null),
      h("div", { class: "row", style: "margin-top:14px" },
        h("span", { class: "muted small" }, "Was this useful?"),
        feedbackButtons(data.id)),
    );

    const tabs = [
      { key: "evidence", label: "Evidence", count: data.evidence.length + data.records.length + data.profile.length, render: () => servedItems(data) },
      { key: "trace", label: "Why", count: null, render: () => traceView(data.trace) },
      { key: "agent", label: "What the agent sees", count: null, render: () => data.rendered ? h("pre", { class: "rendered" }, data.rendered) : h("div", { class: "empty" }, "Nothing. The agent is told nothing, which is the right answer when nothing covers the question.") },
    ];
    const body = h("div", {});
    const bar = h("div", { class: "tabs" });
    function show(key) {
      bar.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.key === key));
      body.replaceChildren(tabs.find((tab) => tab.key === key).render());
    }
    bar.replaceChildren(...tabs.map((tab) => h("button", { type: "button", "data-key": tab.key, onclick: () => show(tab.key) },
      tab.label, tab.count !== null ? h("span", { class: "count" }, tab.count) : null)));
    const panel = h("div", { class: "card" }, bar, body);
    show(data.evidence.length || data.records.length || data.profile.length ? "evidence" : "trace");
    return h("div", { class: "stack" }, summary, panel);
  }

  function metric(key, value) {
    return h("div", { class: "metric" }, h("div", { class: "k" }, key), h("div", { class: "v" }, value));
  }

  function feedbackButtons(id) {
    const wrap = h("div", { class: "row" });
    const send = async (useful) => {
      try {
        await api("POST", "feedback", { id, useful });
        wrap.replaceChildren(h("span", { class: `pill ${useful ? "good" : "bad"}` }, useful ? "Marked useful" : "Marked not useful"));
      } catch (error) { toast(error.message); }
    };
    wrap.append(
      h("button", { class: "btn sm good", type: "button", onclick: () => send(true) }, "Useful"),
      h("button", { class: "btn sm bad", type: "button", onclick: () => send(false) }, "Not useful"));
    return wrap;
  }

  function servedItems(data) {
    const parts = [];
    if (!data.evidence.length && !data.records.length && !data.profile.length) {
      return h("div", { class: "empty" }, h("div", { class: "big" }, "Nothing served"), "Open “Why” to see every candidate that was considered and the rule that cut it.");
    }
    data.evidence.forEach((item) => parts.push(evidenceCard(item)));
    if (data.records.length) {
      parts.push(h("div", { class: "section-title" }, "History records"));
      parts.push(h("div", {}, data.records.map(recordCard)));
    }
    if (data.profile.length) {
      parts.push(h("div", { class: "section-title" }, "Profile"));
      parts.push(h("div", {}, data.profile.map((item) => h("div", { class: "record" },
        h("div", {}, item.statement),
        h("div", { class: "row small muted" }, h("span", { class: "pill" }, item.category), `confidence ${item.confidence.toFixed(2)}`)))));
    }
    return h("div", { class: "stack" }, parts);
  }

  function lineSpan(citation) {
    const match = /:(\d+)(?:-(\d+))?$/.exec(citation);
    return match ? Number(match[1]) : 0;
  }

  function evidenceCard(item, extra) {
    const start = lineSpan(item.citation);
    const lines = (item.excerpt || "").split("\n");
    const terms = (item.matched || []).map((term) => term.toLowerCase());
    const code = h("pre", { class: "code" }, lines.map((line, index) => {
      const lower = line.toLowerCase();
      const hit = terms.some((term) => term && lower.includes(term));
      return h("span", { class: `ln${hit ? " hit" : ""}`, "data-n": start ? start + index : "" }, line || " ");
    }));
    return h("div", { class: "card evidence" },
      h("div", { class: "evidence-head" },
        h("span", { class: "pill accent" }, item.source_type),
        h("span", { class: "pill" }, item.project_key || item.project),
        item.cross_project ? h("span", { class: "pill teal" }, "from another project") : null,
        extra || null,
        h("span", { class: "evidence-title" }, item.heading || item.title || "")),
      h("div", { class: "citation" }, h("span", { class: "mono" }, item.citation), copyButton(item.citation)),
      code,
      item.matched && item.matched.length
        ? h("div", { class: "evidence-foot" }, "matched", item.matched.map((term) => h("span", { class: "term" }, term)),
          item.score !== undefined ? h("span", { class: "spacer" }) : null,
          item.score !== undefined ? `score ${Number(item.score).toFixed(2)}` : null)
        : null);
  }

  function recordCard(item) {
    return h("div", { class: "record" },
      h("div", { class: "row" },
        h("span", { class: "pill teal" }, item.type),
        h("span", { class: "pill" }, item.project_key),
        item.cross_project ? h("span", { class: "pill accent" }, "from another project") : null,
        h("span", { class: "muted small" }, when(item.occurred_at))),
      h("div", {}, item.what),
      item.why ? h("div", { class: "muted" }, "Why: ", item.why) : null,
      h("div", { class: "citation", style: "padding:0" }, h("span", { class: "mono small" }, item.source_url), copyButton(item.source_url)));
  }

  function traceView(trace) {
    trace = trace || {};
    const blocks = [];
    if (trace.decision) {
      const d = trace.decision;
      blocks.push(h("div", {},
        h("h3", {}, "Gate"),
        h("dl", { class: "kv" },
          h("dt", {}, "Task type"), h("dd", {}, d.task_type || "–"),
          h("dt", {}, "Decision"), h("dd", {}, d.reason || "–"),
          h("dt", {}, "Search history records"), h("dd", {}, d.inject_episodic ? "yes" : "no"),
          h("dt", {}, "Serve profile"), h("dd", {}, d.inject_profile ? "yes" : "no"),
          h("dt", {}, "Entities"), h("dd", {}, (d.entities || []).join(", ") || "none"))));
    }
    const evidence = (trace.evidence || []);
    const candidates = evidence.filter((step) => step.stage === "evidence");
    const semantic = evidence.filter((step) => step.stage === "semantic");
    const notes = evidence.filter((step) => step.stage !== "evidence" && step.stage !== "semantic");
    blocks.push(h("div", {},
      h("h3", {}, "Passages considered ", h("span", { class: "muted" }, `(${candidates.length})`)),
      notes.map((note) => h("div", { class: "muted small", style: "margin-bottom:8px" }, note.reason || JSON.stringify(note))),
      candidates.length ? h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, "Outcome"), h("th", {}, "Source"), h("th", {}, "Coverage"), h("th", {}, "Matched"))),
        h("tbody", {}, candidates.map((step) => h("tr", {},
          h("td", { class: step.outcome === "served" ? "outcome-served" : "outcome-cut" }, step.outcome),
          h("td", { class: "mono small" }, h("div", {}, step.project), h("div", { class: "muted truncate", title: step.uri }, (step.uri || "").split(/[\\/]/).slice(-2).join("/"))),
          h("td", { class: "num" }, h("span", { class: "cov" }, h("span", { style: `width:${Math.min(100, (step.coverage || 0) * 100)}%` })), (step.coverage || 0).toFixed(2)),
          h("td", {}, (step.matched || []).map((term) => h("span", { class: "term", style: "margin-right:4px" }, term)))))))
        : h("div", { class: "muted" }, "No passage shared enough of the question's words to be considered.")));
    if (semantic.length) {
      blocks.push(h("div", {},
        h("h3", {}, "Matched by meaning"),
        h("table", {}, h("tbody", {}, semantic.map((step) => h("tr", {},
          h("td", { class: step.outcome === "served" ? "outcome-served" : "outcome-cut" }, step.outcome),
          h("td", { class: "mono small truncate", title: step.uri || "" }, step.uri || ""),
          h("td", { class: "num" }, step.similarity !== undefined ? step.similarity.toFixed(3) : "")))))));
    }
    if ((trace.retrieval || []).length) {
      blocks.push(h("div", {},
        h("h3", {}, "History records considered ", h("span", { class: "muted" }, `(${trace.retrieval.length})`)),
        h("pre", { class: "rendered" }, trace.retrieval.map((step) => JSON.stringify(step)).join("\n"))));
    }
    return h("div", { class: "stack" }, blocks);
  }

  // --- search ----------------------------------------------------------------
  async function renderSearch(root) {
    const query = h("input", { class: "input", placeholder: "What are you looking for?", spellcheck: "false" });
    const project = h("input", { class: "input", placeholder: "Only this project key (optional)", spellcheck: "false" });
    const button = h("button", { class: "btn primary", type: "submit" }, "Search");
    const results = h("div", { class: "stack" });
    async function submit(event) {
      event.preventDefault();
      if (!query.value.trim()) return;
      button.disabled = true;
      try {
        const data = await api("POST", "search", { query: query.value.trim(), project: project.value.trim(), limit: 8 });
        const head = h("div", { class: "row muted small" },
          `${data.results.length} result${data.results.length === 1 ? "" : "s"} in ${ms(data.latency_ms)}`,
          h("span", { class: `pill ${data.semantic ? "teal" : ""}` }, data.semantic ? "words + meaning" : "words only (semantic search is off)"));
        results.replaceChildren(head, ...(data.results.length ? data.results.map((item) => evidenceCard(
          { ...item, project_key: item.project, matched: [] },
          h("span", { class: `pill ${item.via === "meaning" ? "teal" : "good"}` }, item.via === "meaning" ? `by meaning · ${item.similarity.toFixed(2)}` : "by words")))
          : [h("div", { class: "card empty" }, h("div", { class: "big" }, "Nothing indexed says that"), "Try other words, or turn on semantic search.")]));
      } catch (error) { if (error.message !== "unauthorized") results.replaceChildren(h("div", { class: "card empty" }, error.message)); }
      finally { button.disabled = false; }
    }
    root.replaceChildren(
      h("form", { class: "card ask-card", onsubmit: submit }, query, h("div", { class: "ask-row" },
        h("div", { class: "small muted" }, "Unlike Ask, this shows close neighbours too, each labelled with how it was found."), project, button)),
      results);
    query.focus();
  }

  // --- history ---------------------------------------------------------------
  async function renderHistory(root) {
    const filters = state.history;
    const seg = h("div", { class: "seg" }, [["", "All"], ["yes", "Served"], ["no", "Silent"]].map(([value, label]) =>
      h("button", { type: "button", class: filters.served === value ? "on" : "", onclick: () => { filters.served = value; route(); } }, label)));
    const sourceSelect = h("select", { class: "input", style: "width:auto", onchange: (event) => { filters.source = event.target.value; route(); } },
      [["", "Every source"], ["hook", "Hook"], ["ui", "Dashboard"], ["mcp:get_context", "MCP"], ["cli", "CLI"]].map(([value, label]) =>
        h("option", { value, selected: filters.source === value }, label)));
    const params = new URLSearchParams({ limit: "200" });
    if (filters.served) params.set("served", filters.served);
    if (filters.source) params.set("source", filters.source);
    const { items } = await api("GET", `history?${params}`);
    const table = items.length ? h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "When"), h("th", {}, "Question"), h("th", {}, "Project"), h("th", {}, "Source"), h("th", { class: "num" }, "Served"), h("th", { class: "num" }, "Tokens"), h("th", { class: "num" }, "Latency"), h("th", {}, ""))),
      h("tbody", {}, items.map((item) => h("tr", { class: "clickable", onclick: () => openInteraction(item.id) },
        h("td", { class: "muted small", style: "white-space:nowrap" }, when(item.created_at)),
        h("td", {}, h("div", { class: "truncate" }, item.prompt || h("span", { class: "muted" }, `(${item.prompt_words} words, text not kept)`))),
        h("td", { class: "small" }, item.project || "–"),
        h("td", {}, h("span", { class: "pill" }, item.source)),
        h("td", { class: "num" }, item.served ? h("span", { class: "pill good" }, item.served) : h("span", { class: "muted" }, "0")),
        h("td", { class: "num" }, num(item.tokens)),
        h("td", { class: "num muted" }, ms(item.latency_ms)),
        h("td", {}, item.feedback === true ? h("span", { class: "pill good" }, "useful") : item.feedback === false ? h("span", { class: "pill bad" }, "not useful") : null)))))
      : h("div", { class: "empty" }, h("div", { class: "big" }, "No interactions yet"), "Ask something, or use your agent with the hook installed.");
    root.replaceChildren(h("div", { class: "row" }, seg, sourceSelect, h("span", { class: "spacer" }), h("span", { class: "muted small" }, `${items.length} shown`)),
      h("div", { class: "card", style: "padding:6px 8px" }, table));
    // #/history?id=<id> opens one interaction; ?id=latest the newest shown.
    const wanted = new URLSearchParams(location.hash.split("?")[1] || "").get("id");
    if (wanted) openInteraction(wanted === "latest" && items.length ? items[0].id : wanted);
  }

  async function openInteraction(id) {
    const drawer = $("drawer");
    const body = $("drawer-body");
    drawer.hidden = false;
    body.replaceChildren(h("div", { class: "skeleton" }));
    try {
      const item = await api("GET", `history/${id}`);
      $("drawer-title").textContent = item.prompt ? "Interaction" : "Interaction (hashed)";
      body.replaceChildren(
        h("div", { class: "card" },
          h("div", { style: "font-size:15px;margin-bottom:10px" }, item.prompt || "The prompt text was not kept (history mode or excluded project)."),
          h("dl", { class: "kv" },
            h("dt", {}, "When"), h("dd", {}, new Date(item.created_at).toLocaleString()),
            h("dt", {}, "Source"), h("dd", {}, item.source),
            h("dt", {}, "Project"), h("dd", {}, item.project || "–"),
            h("dt", {}, "Folder"), h("dd", { class: "mono small" }, item.cwd || "–"),
            h("dt", {}, "Decision"), h("dd", {}, item.reason || "–"),
            h("dt", {}, "Served"), h("dd", {}, `${item.evidence} excerpts, ${item.records} records, ${item.profile} statements · ${num(item.tokens)} tokens · ${ms(item.latency_ms)}`),
            h("dt", {}, "Delivered"), h("dd", {}, item.delivered === null ? "–" : item.delivered ? "yes" : "no"),
            h("dt", {}, "Version"), h("dd", {}, `${item.version || "–"} · ${item.embedder || "–"}`)),
          h("div", { class: "row", style: "margin-top:14px" }, h("span", { class: "muted small" }, "Feedback"),
            item.feedback === null ? feedbackButtons(item.id) : h("span", { class: `pill ${item.feedback ? "good" : "bad"}` }, item.feedback ? "useful" : "not useful"),
            item.feedback_note ? h("span", { class: "muted small" }, item.feedback_note) : null)),
        h("div", { class: "section-title" }, "What the agent received"),
        item.rendered ? h("pre", { class: "rendered" }, item.rendered) : h("div", { class: "card empty" }, "Nothing was served."),
        h("div", { class: "section-title" }, "Why"),
        h("div", { class: "card" }, traceView(item.trace)));
    } catch (error) { body.replaceChildren(h("div", { class: "card empty" }, error.message)); }
  }
  $("drawer-close").addEventListener("click", () => { $("drawer").hidden = true; });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") $("drawer").hidden = true; });

  // --- knowledge ---------------------------------------------------------------
  async function renderKnowledge(root) {
    const [data, overview] = await Promise.all([api("GET", "knowledge"), loadOverview()]);
    const tiles = h("div", { class: "grid-tiles" },
      tile("Documents", num(overview.documents), "files, pages, sessions, commits"),
      tile("Passages", num(overview.passages), "citable excerpts"),
      tile("History records", num(overview.records), "decisions and failures from git"),
      tile("Projects", num(data.projects), "folders and repositories"));
    const sync = data.daemon || {};
    const last = sync.last_sync || {};
    const syncButton = h("button", {
      class: "btn sm", type: "button", disabled: state.demo || sync.syncing,
      onclick: async () => {
        try { await api("POST", "sync"); toast("Sync started in the background"); setTimeout(route, 1500); } catch (error) { toast(error.message); }
      },
    }, sync.syncing ? "Syncing…" : "Sync now");
    root.replaceChildren(
      tiles,
      h("div", { class: "grid-2" },
        h("div", { class: "card" }, h("h2", {}, "Passages by source"), bars(data.by_type, "passages")),
        h("div", { class: "card" },
          h("h2", {}, "Sources"),
          h("div", { class: "row", style: "margin-bottom:12px" },
            h("span", { class: `dot ${sync.syncing ? "busy" : "ok"}` }),
            h("span", {}, sync.syncing ? "Sync running" : last.at ? `Last sync ${when(last.at)}` : state.demo ? "Demo workspace, indexed at start" : "No sync yet in this session"),
            h("span", { class: "spacer" }), syncButton),
          last.error ? h("div", { class: "pill bad", style: "margin-bottom:10px" }, last.error) : null,
          last.summary ? h("div", { class: "muted small", style: "margin-bottom:10px" }, last.summary) : null,
          sync.sync_minutes ? h("div", { class: "muted small", style: "margin-bottom:12px" }, `Runs every ${sync.sync_minutes} minutes at low priority.`) : null,
          data.roots.length ? h("table", {}, h("tbody", {}, data.roots.map((root) => h("tr", {},
            h("td", { class: "mono small" }, root.path), h("td", {}, h("span", { class: "pill" }, root.kind)), h("td", { class: "muted small num" }, `depth ${root.depth}`)))))
            : h("div", { class: "muted" }, state.demo ? "The fictional workspace: four projects, a git history, a recorded session and a captured web page." : "No roots configured. Add one with: projectmind sources add <folder>"))),
      h("div", { class: "card" }, h("h2", {}, "Largest projects ", h("span", { class: "muted" }, "by passages")), bars(data.by_project, "passages")));
  }

  function tile(label, value, note) {
    return h("div", { class: "card tile" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value), note ? h("div", { class: "note" }, note) : null);
  }

  function bars(rows, key) {
    if (!rows.length) return h("div", { class: "muted" }, "Nothing indexed yet.");
    const max = Math.max(...rows.map((row) => row[key])) || 1;
    return h("div", { class: "bars" }, rows.map((row) => h("div", { class: "bar-row", title: `${row.name}: ${num(row.passages)} passages in ${num(row.documents)} documents` },
      h("div", { class: "name" }, row.name),
      h("div", { class: "bar-track" }, h("div", { class: "bar-fill", style: `width:${(row[key] / max) * 100}%` })),
      h("div", { class: "val" }, num(row[key])))));
  }

  // --- profile ---------------------------------------------------------------
  async function renderProfile(root) {
    const status = state.profileStatus;
    const counts = (state.overview && state.overview.profile) || {};
    const seg = h("div", { class: "seg" }, [["proposed", "Awaiting review"], ["active", "Active"], ["rejected", "Rejected"], ["all", "All"]].map(([value, label]) =>
      h("button", { type: "button", class: status === value ? "on" : "", onclick: () => { state.profileStatus = value; route(); } },
        label, value !== "all" && counts[value] !== undefined ? h("span", { class: "muted" }, ` ${counts[value]}`) : null)));
    const { items } = await api("GET", `profile?status=${status}`);
    const list = h("div", { class: "card" }, items.length ? items.map((item) => statementRow(item)) :
      h("div", { class: "empty" }, h("div", { class: "big" }, status === "proposed" ? "Nothing awaiting review" : "No statements here"),
        status === "proposed" ? "New proposals appear after: projectmind review run" : ""));
    root.replaceChildren(h("div", { class: "row" }, seg, h("span", { class: "spacer" }),
      h("span", { class: "muted small" }, "Statements are served only after you approve them.")), list);
  }

  function statementRow(item) {
    const actions = h("div", { class: "row" });
    const decide = async (verdict) => {
      try {
        const result = await api("POST", `profile/${item.id}/${verdict}`);
        actions.replaceChildren(h("span", { class: `pill ${result.status === "active" ? "good" : "bad"}` }, result.status));
        loadOverview().catch(() => {});
      } catch (error) { toast(error.message); }
    };
    if (item.status === "proposed") {
      actions.append(h("button", { class: "btn sm good", type: "button", onclick: () => decide("approve") }, "Approve"),
        h("button", { class: "btn sm bad", type: "button", onclick: () => decide("reject") }, "Reject"));
    } else {
      actions.append(h("span", { class: `pill ${item.status === "active" ? "good" : item.status === "rejected" ? "bad" : ""}` }, item.status));
    }
    return h("div", { class: "statement" },
      h("div", {},
        h("div", { class: "text" }, item.statement),
        h("div", { class: "row small muted", style: "margin-top:6px" },
          h("span", { class: "pill" }, item.category.replace(/_/g, " ")),
          h("span", {}, h("span", { class: "meter" }, h("span", { style: `width:${item.confidence * 100}%` })), ` ${item.confidence.toFixed(2)}`),
          item.entities.slice(0, 4).map((entity) => h("span", { class: "term" }, entity)),
          item.evidence.length ? h("span", {}, `${item.evidence.length} evidence link${item.evidence.length === 1 ? "" : "s"}`) : null)),
      actions);
  }

  // --- evaluation ------------------------------------------------------------
  async function renderEval(root) {
    const data = await api("GET", "eval");
    const report = data.real;
    if (!report) {
      root.replaceChildren(h("div", { class: "card empty" }, h("div", { class: "big" }, "No real-data report yet"),
        "Write questions about your own material (see eval/real.example.yaml), then run: ",
        h("span", { class: "mono" }, `projectmind eval real --save ${data.path}`)));
      return;
    }
    let filter = "all";
    const list = h("div", {});
    const outcomes = report.outcomes || [];
    function draw() {
      const shown = outcomes.filter((o) => filter === "all" || (filter === "fail" ? !o.passed : o.passed));
      list.replaceChildren(...shown.map((o) => h("div", { class: "outcome" },
        h("div", { class: `mark ${o.passed ? "pass" : "fail"}` }, o.passed ? "✓" : "✕"),
        h("div", {}, h("div", {}, o.prompt), h("div", { class: "muted small" }, o.reason),
          o.served && o.served.length ? h("div", { class: "mono small muted truncate", title: o.served.join("\n") }, o.served[0]) : null),
        h("div", { class: "row" }, h("span", { class: `pill ${o.expect === "nothing" ? "warn" : ""}` }, o.expect === "nothing" ? "should be silent" : "answerable"),
          h("span", { class: "muted small" }, ms(o.latency_ms))))));
    }
    const seg = h("div", { class: "seg" }, [["all", "All"], ["fail", "Misses"], ["pass", "Passes"]].map(([value, label]) =>
      h("button", { type: "button", class: value === filter ? "on" : "", onclick: (event) => {
        filter = value; seg.querySelectorAll("button").forEach((b) => b.classList.remove("on")); event.target.classList.add("on"); draw();
      } }, label)));
    draw();
    root.replaceChildren(
      h("div", { class: "grid-tiles" },
        tile("Hit rate", pct(report.hit_rate), `answerable questions (${report.answerable})`),
        tile("Silence", pct(report.silence_rate), `absent topics left unanswered (${report.silent})`),
        tile("Served precision", pct(report.served_precision), "served items that were expected"),
        tile("p95 latency", ms(report.p95_latency_ms), `${num(Math.round(report.mean_tokens))} tokens per answer on average`)),
      h("div", { class: "card" },
        h("div", { class: "row", style: "margin-bottom:8px" },
          h("h2", { style: "margin:0" }, `${report.name || "Real-data set"} `, h("span", { class: "muted" }, `· ${report.queries} questions · measured ${when(data.measured_at)}`)),
          h("span", { class: "spacer" }), seg),
        list));
  }

  // --- tour (demo) ------------------------------------------------------------
  async function renderTour(root) {
    const button = h("button", { class: "btn primary", type: "button" }, "Run the tour");
    const out = h("div", { class: "card" }, h("div", { class: "empty" }, "Seven questions on a fictional workspace, from a mechanical edit that should be skipped to a topic that is not there at all."));
    button.addEventListener("click", async () => {
      button.disabled = true;
      button.replaceChildren(h("span", { class: "spinner" }), "Running");
      try {
        const { steps } = await api("GET", "tour");
        const passed = steps.filter((s) => s.passed).length;
        out.replaceChildren(
          h("div", { class: "row", style: "margin-bottom:16px" },
            h("span", { class: `pill ${passed === steps.length ? "good" : "bad"}` }, `${passed} of ${steps.length} steps behaved as expected`)),
          ...steps.map((s, i) => h("div", { class: "step" },
            h("div", { class: "row" }, h("div", { class: `mark ${s.passed ? "pass" : "fail"}` }, s.passed ? "✓" : "✕"),
              h("b", {}, `${i + 1}. ${s.title}`), h("span", { class: "pill" }, s.project), h("span", { class: "spacer" }),
              h("span", { class: "muted small" }, `${num(s.tokens)} tokens · ${ms(s.latency_ms)}`)),
            h("blockquote", {}, s.prompt),
            h("div", { class: "muted small" }, "Shows: ", s.shows),
            s.skipped ? h("div", { class: "pill" }, "skipped: a mechanical edit, nothing was searched")
              : s.citations.length ? s.citations.map((c, j) => h("div", { class: "record" },
                h("div", { class: "citation", style: "padding:0" }, h("span", { class: "mono small" }, c)),
                h("pre", { class: "rendered", style: "max-height:180px" }, s.excerpts[j] || "")))
                : h("div", { class: "pill warn" }, "nothing served"))));
      } catch (error) { toast(error.message); }
      finally { button.disabled = false; button.replaceChildren("Run again"); }
    });
    root.replaceChildren(h("div", { class: "row" }, button, h("span", { class: "muted small" }, "Each step goes through the same front door as the hook.")), out);
    if (new URLSearchParams(location.hash.split("?")[1] || "").get("run")) button.click();
  }

  // --- system ----------------------------------------------------------------
  async function renderSystem(root) {
    const o = await loadOverview();
    const d = o.daemon || {};
    const profile = Object.entries(o.profile || {}).map(([k, v]) => `${v} ${k}`).join(", ") || "none";
    root.replaceChildren(
      h("div", { class: "grid-2" },
        h("div", { class: "card" }, h("h2", {}, "Daemon"), h("dl", { class: "kv" },
          h("dt", {}, "Version"), h("dd", {}, o.version),
          h("dt", {}, "Mode"), h("dd", {}, o.demo ? "demo workspace (nothing personal)" : "your memory"),
          h("dt", {}, "Listening"), h("dd", { class: "mono" }, `127.0.0.1:${d.port}`),
          h("dt", {}, "Process"), h("dd", {}, `pid ${d.pid}`),
          h("dt", {}, "Up since"), h("dd", {}, new Date(o.started_at).toLocaleString()),
          h("dt", {}, "Background sync"), h("dd", {}, d.sync_minutes ? `every ${d.sync_minutes} min` : "off"))),
        h("div", { class: "card" }, h("h2", {}, "Retrieval"), h("dl", { class: "kv" },
          h("dt", {}, "Semantic search"), h("dd", {}, o.semantic.enabled
            ? h("span", { class: "pill teal" }, `on · ${num(o.semantic.loaded)} vectors · threshold ${o.semantic.threshold}`)
            : h("span", { class: "pill" }, "off (projectmind semantic enable)")),
          h("dt", {}, "Embedder"), h("dd", {}, o.embedder),
          h("dt", {}, "History"), h("dd", {}, `${o.history_mode} · ${num(o.interactions)} interactions`),
          h("dt", {}, "Profile"), h("dd", {}, profile),
          h("dt", {}, "Home"), h("dd", { class: "mono small" }, o.home)))),
      h("div", { class: "card" }, h("h2", {}, "Security"), h("div", { class: "muted" },
        "This page is served only on 127.0.0.1. Every request carries a token that lives in a file only your account can read; it reached this tab in the URL fragment, which is never sent over the network, and is kept in this tab's session storage. The page loads nothing from the internet.")));
  }

  // --- gate: no token ----------------------------------------------------------
  function showGate(message) {
    const input = h("input", { class: "input mono", placeholder: "paste the token", spellcheck: "false" });
    $("view-title").textContent = "Connect";
    $("view-sub").textContent = "";
    $("view").replaceChildren(h("div", { class: "gate" }, h("form", {
      class: "card stack",
      onsubmit: (event) => {
        event.preventDefault();
        token = input.value.trim();
        try { sessionStorage.setItem(TOKEN_KEY, token); } catch (_) { /* ignore */ }
        start();
      },
    },
      h("h2", {}, "Connect to your daemon"),
      h("div", { class: "muted" }, message || "Open the dashboard with ", h("span", { class: "mono" }, "projectmind ui"), ", or paste the token from ", h("span", { class: "mono" }, "projectmind daemon token"), "."),
      input, h("button", { class: "btn primary", type: "submit" }, "Connect"))));
    $("daemon-dot").className = "dot bad";
    $("daemon-text").textContent = "not connected";
  }

  async function start() {
    if (!token) { showGate(); return; }
    try {
      await loadOverview();
    } catch (error) {
      if (error.message !== "unauthorized") showGate(`Could not reach the daemon: ${error.message}`);
      return;
    }
    route();
    setInterval(() => { loadOverview().catch(() => {}); }, 30000);
  }
  start();
})();
