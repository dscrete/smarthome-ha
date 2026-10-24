/* Locally hosted, dependency-free Home Assistant jobs panel. */
class SmartHomeJobsPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.jobs = [];
    this.stale = true;
    this.refreshQueued = false;
    this.refreshSequence = 0;
    this.connectionGeneration = 0;
    this.notice = null;
    this.busyJobs = new Set();
    this.connection = null;
    this.unsubscribe = null;
    this.connecting = false;
    this.onReady = null;
    this.onDisconnected = null;
    this.timer = null;
  }

  set hass(value) {
    if (this._hass === value) return;
    this._hass = value;
    if (this.isConnected) this.connect();
  }

  get hass() { return this._hass; }

  set narrow(value) {
    const next = Boolean(value);
    if (this._narrow === next) return;
    this._narrow = next;
    if (this.isConnected) this.render();
  }

  get narrow() { return this._narrow || false; }

  connectedCallback() {
    this.render();
    this.connect();
    this.timer = window.setInterval(() => this.updateCountdowns(), 1000);
  }

  disconnectedCallback() {
    this.connectionGeneration += 1;
    this.refreshSequence += 1;
    this.connecting = false;
    if (this.unsubscribe) Promise.resolve(this.unsubscribe()).catch(() => {});
    this.unsubscribe = null;
    this.removeConnectionListeners();
    this.connection = null;
    if (this.timer) window.clearInterval(this.timer);
    this.timer = null;
  }

  async connect() {
    if (!this.isConnected || !this.hass?.connection) return;
    if (this.connection === this.hass.connection && (this.unsubscribe || this.connecting)) return;
    if (this.unsubscribe) Promise.resolve(this.unsubscribe()).catch(() => {});
    this.removeConnectionListeners();
    const generation = ++this.connectionGeneration;
    this.connection = this.hass.connection;
    this.unsubscribe = null;
    this.connecting = true;
    const connection = this.connection;
    this.onReady = () => {
      if (generation === this.connectionGeneration && connection === this.connection) {
        this.refresh().catch(() => {});
      }
    };
    this.onDisconnected = () => {
      if (generation !== this.connectionGeneration || connection !== this.connection) return;
      this.refreshSequence += 1;
      this.showError("Home Assistant disconnected");
    };
    this.connection.addEventListener?.("ready", this.onReady);
    this.connection.addEventListener?.("disconnected", this.onDisconnected);
    this.stale = true;
    this.render();
    try {
      const unsubscribe = await connection.subscribeMessage(
        () => {
          if (generation === this.connectionGeneration && connection === this.connection) this.scheduleRefresh();
        }, { type: "smarthome_jobs/subscribe" },
      );
      if (!this.isConnected || generation !== this.connectionGeneration || connection !== this.connection) {
        await unsubscribe();
        return;
      }
      this.unsubscribe = unsubscribe;
      await this.refresh();
    } catch (error) {
      if (generation === this.connectionGeneration) this.showError(error);
    } finally {
      if (generation === this.connectionGeneration) this.connecting = false;
    }
  }

  removeConnectionListeners() {
    if (this.connection && this.onReady) this.connection.removeEventListener?.("ready", this.onReady);
    if (this.connection && this.onDisconnected) this.connection.removeEventListener?.("disconnected", this.onDisconnected);
    this.onReady = null;
    this.onDisconnected = null;
  }

  scheduleRefresh() {
    if (this.refreshQueued) return;
    this.refreshQueued = true;
    queueMicrotask(() => {
      this.refreshQueued = false;
      this.refresh().catch(() => {});
    });
  }

  async refresh() {
    if (!this.connection) return;
    const sequence = ++this.refreshSequence;
    const generation = this.connectionGeneration;
    const connection = this.connection;
    if (connection.connected === false) {
      const error = new Error("Home Assistant disconnected");
      if (generation === this.connectionGeneration && sequence === this.refreshSequence) this.showError(error);
      throw error;
    }
    try {
      const result = await connection.sendMessagePromise({
        type: "smarthome_jobs/list",
      });
      if (generation !== this.connectionGeneration || sequence !== this.refreshSequence || connection !== this.connection) return;
      this.jobs = Array.isArray(result.jobs) ? result.jobs : [];
      this.stale = false;
      if (this.notice?.startsWith("Jobs may be out of date:")) this.notice = null;
      this.render();
    } catch (error) {
      if (generation === this.connectionGeneration && sequence === this.refreshSequence) this.showError(error);
      throw error;
    }
  }

  showError(error) {
    this.stale = true;
    this.notice = `Jobs may be out of date: ${this.errorText(error)}. Controls are disabled until a refresh succeeds.`;
    this.render();
  }

  showNotice(message) {
    this.notice = message;
    const status = this.shadowRoot.querySelector("[role=status]");
    if (status) status.textContent = message;
  }

  errorText(error) {
    return String(error?.message || error || "connection unavailable").slice(0, 240);
  }

  requestId() {
    if (crypto.randomUUID) return crypto.randomUUID();
    // HA also supports trusted LAN HTTP, where randomUUID is unavailable.
    return Array.from(crypto.getRandomValues(new Uint8Array(16)),
      (byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  async mutate(command, job) {
    if (this.stale || !this.connection || this.busyJobs.has(job.id)) return;
    const connection = this.connection;
    const generation = this.connectionGeneration;
    const isCurrent = () => generation === this.connectionGeneration && connection === this.connection;
    this.busyJobs.add(job.id);
    const button = this.shadowRoot.querySelector(`button[data-job-id="${CSS.escape(job.id)}"]`);
    if (button) button.disabled = true;
    try {
      try {
        await connection.sendMessagePromise({
          type: `smarthome_jobs/${command}`,
          request_id: this.requestId(),
          job_id: job.id,
          expected_revision: job.revision,
        });
      } catch (error) {
        if (!isCurrent()) return;
        // Refresh current revision, but never replay a mutation whose result is uncertain.
        try {
          await this.refresh();
          if (!isCurrent()) return;
          this.showNotice(`Command result: ${this.errorText(error)}. The list was refreshed; the command was not retried.`);
        } catch (refreshError) {
          if (!isCurrent()) return;
          this.showError(`Command result is unknown (${this.errorText(error)}); refresh failed (${this.errorText(refreshError)}).`);
        }
        return;
      }
      if (!isCurrent()) return;
      try {
        await this.refresh();
      } catch (error) {
        if (!isCurrent()) return;
        this.showError(`Command succeeded, but the job list could not be refreshed: ${this.errorText(error)}.`);
      }
    } finally {
      this.busyJobs.delete(job.id);
      const current = this.shadowRoot.querySelector(`button[data-job-id="${CSS.escape(job.id)}"]`);
      if (current) current.disabled = this.stale;
    }
  }

  date(value) {
    const locale = this.hass?.locale?.language || undefined;
    const timeZone = this.hass?.config?.time_zone || undefined;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "Unknown time" : new Intl.DateTimeFormat(locale, {
      dateStyle: "medium", timeStyle: "short", timeZone,
    }).format(date);
  }

  remaining(value) {
    const ms = new Date(value).getTime() - Date.now();
    if (!Number.isFinite(ms)) return "Countdown unavailable";
    if (ms <= 0) return "Due";
    const seconds = Math.ceil(ms / 1000);
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remainder = seconds % 60;
    return hours ? `${hours}h ${minutes}m` : minutes ? `${minutes}m ${remainder}s` : `${remainder}s`;
  }

  label(value) {
    return String(value || "").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  appendText(parent, tag, value, className) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    element.textContent = value == null ? "" : String(value);
    parent.append(element);
    return element;
  }

  render() {
    const root = this.shadowRoot;
    root.replaceChildren();
    const style = document.createElement("style");
    style.textContent = `
      :host { display: block; color: var(--primary-text-color); font-family: var(--paper-font-body1_-_font-family, inherit); }
      main { max-width: 1000px; margin: auto; padding: 24px; }
      .heading { display: flex; align-items: center; gap: 12px; }
      h1 { font-size: 1.5rem; }
      h2 { font-size: 1.15rem; margin-top: 28px; }
      .status { min-height: 1.5em; color: var(--secondary-text-color); }
      .group { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 280px), 1fr)); gap: 12px; }
      .job { min-width: 0; overflow-wrap: anywhere; border: 1px solid var(--divider-color); border-radius: 10px; padding: 16px; background: var(--card-background-color); }
      .job h3 { margin: 0 0 8px; }
      .meta { color: var(--secondary-text-color); margin: 4px 0; }
      .actions { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
      button { font: inherit; padding: 8px 14px; border: 0; border-radius: 6px; background: var(--primary-color); color: var(--text-primary-color, #fff); cursor: pointer; }
      button[disabled] { opacity: .5; cursor: default; }
      .empty { color: var(--secondary-text-color); }
      @media (max-width: 600px) { main { padding: 16px; } .job { padding: 12px; } }
    `;
    root.append(style);
    const main = document.createElement("main");
    const heading = document.createElement("div");
    heading.className = "heading";
    const menu = document.createElement("button");
    menu.className = "menu";
    menu.hidden = !this.narrow;
    menu.type = "button";
    menu.textContent = "Menu";
    menu.setAttribute("aria-label", "Open Home Assistant sidebar");
    menu.addEventListener("click", () => this.dispatchEvent(new Event("hass-toggle-menu", { bubbles: true, composed: true })));
    heading.append(menu);
    this.appendText(heading, "h1", "SmartHome jobs");
    main.append(heading);
    const status = this.appendText(main, "p", this.notice || (this.stale ? "Connecting to job history…" : "Jobs are up to date."), "status");
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    if (this.stale) {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.textContent = "Retry refresh";
      retry.addEventListener("click", () => {
        if (!this.unsubscribe) this.connect();
        else this.refresh().catch((error) => this.showError(error));
      });
      main.append(retry);
    }

    const active = [];
    const history = [];
    const terminal = new Set(["completed", "failed", "unknown", "missed", "expired", "dismissed", "cancelled"]);
    for (const job of [...this.jobs].sort((a, b) => String(b.due_at).localeCompare(String(a.due_at)))) {
      (terminal.has(job.status) ? history : active).push(job);
    }
    active.reverse();
    if (!this.jobs.length) this.appendText(main, "p", this.stale ? "History is unavailable until a connection is restored." : "No jobs to show.", "empty");
    this.renderCollection(main, "Active", active);
    this.renderCollection(main, "History", history);
    root.append(main);
  }

  renderCollection(main, title, jobs) {
    this.appendText(main, "h2", title);
    if (!jobs.length) {
      this.appendText(main, "p", title === "Active" ? "No active jobs." : "No retained history." , "empty");
      return;
    }
    const kinds = new Map();
    for (const job of jobs) {
      const kind = job.kind || "other";
      if (!kinds.has(kind)) kinds.set(kind, []);
      kinds.get(kind).push(job);
    }
    for (const [kind, entries] of [...kinds].sort(([a], [b]) => {
      const order = { timer: 0, scheduled_action: 1 };
      return (order[a] ?? 2) - (order[b] ?? 2) || a.localeCompare(b);
    })) {
      const kindTitle = ({ timer: "Timers", scheduled_action: "Scheduled actions" })[kind] || this.label(kind) || "Other jobs";
      this.appendText(main, "h3", kindTitle);
      const group = document.createElement("section");
      group.className = "group";
      group.setAttribute("aria-label", kindTitle);
      for (const job of entries) group.append(this.renderJob(job));
      main.append(group);
    }
  }

  renderJob(job) {
    const card = document.createElement("article");
    card.className = "job";
    card.dataset.jobId = job.id;
    this.appendText(card, "h3", job.name || this.label(job.kind));
    this.appendText(card, "p", `${this.label(job.status)} · ${this.date(job.due_at)}`, "meta");
    if (job.outcome) this.appendText(card, "p", `Outcome: ${this.label(job.outcome)}`, "meta");
    if (job.status === "pending" || job.status === "due") {
      const countdown = this.appendText(card, "p", this.remaining(job.due_at), "meta");
      countdown.dataset.countdown = job.id;
    }
    if (job.kind === "scheduled_action") {
      const payload = job.payload || {};
      this.appendText(card, "p", `Action: ${this.label(payload.action_id || "Unknown action")} (${payload.capability_id || "unknown capability"})`, "meta");
      if (payload.target && typeof payload.target === "object") {
        const target = Object.entries(payload.target).map(([key, value]) => {
          const id = typeof value === "string" ? value : JSON.stringify(value);
          const entity = typeof value === "string" ? this.hass?.states?.[value] : null;
          const friendly = entity?.attributes?.friendly_name;
          return `${this.label(key)}: ${friendly ? `${friendly} (${id})` : id}`;
        }).join(", ");
        if (target) this.appendText(card, "p", `Target: ${target}`, "meta");
      }
    }
    if (job.kind === "timer" && job.status === "completed" && job.outcome === "handed_off") {
      this.appendText(card, "p", "The timer alert was submitted to the satellite. Playback and dismissal were not confirmed.", "meta");
    }
    if (job.source_satellite) {
      const sourceName = this.hass?.states?.[job.source_satellite]?.attributes?.friendly_name || job.source_satellite;
      this.appendText(card, "p", `Satellite: ${sourceName}`, "meta");
    }
    const actions = document.createElement("div");
    actions.className = "actions";
    if (["pending", "due"].includes(job.status)) this.addButton(actions, "Cancel", job, "cancel");
    if (job.kind === "timer" && job.status === "sounding") this.addButton(actions, "Dismiss timer", job, "timer_dismiss");
    if (actions.childElementCount) card.append(actions);
    return card;
  }

  addButton(parent, title, job, command) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = title;
    button.dataset.jobId = job.id;
    button.disabled = this.stale || this.busyJobs.has(job.id);
    button.setAttribute("aria-label", `${title}: ${job.name || this.label(job.kind)}`);
    button.addEventListener("click", () => this.mutate(command, job));
    parent.append(button);
  }

  updateCountdowns() {
    if (this.stale) return;
    for (const job of this.jobs) {
      const label = this.shadowRoot.querySelector(`[data-countdown="${CSS.escape(job.id)}"]`);
      if (label) label.textContent = this.remaining(job.due_at);
    }
  }
}

customElements.define("smarthome-jobs-panel", SmartHomeJobsPanel);
