/* Locally hosted, dependency-free Home Assistant jobs panel. */
class SmartHomeJobsPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.jobs = [];
    this.schedules = [];
    this.outputs = [];
    this.defaultAlertOutput = "";
    this.snoozeSeconds = 540;
    this.drafts = { create: { kind: "timer", scheduleKind: "relative", seconds: "300", name: "", time: "", date: "", weekdays: "0", day: "1", every: "1", unit: "days", anchor: "", timezone: "", message: "", output: "" }, edit: {} };
    this.editingScheduleId = null;
    this.createBusy = false;
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
        include_schedules: true,
      });
      if (generation !== this.connectionGeneration || sequence !== this.refreshSequence || connection !== this.connection) return;
      this.jobs = Array.isArray(result.jobs) ? result.jobs : [];
      this.schedules = Array.isArray(result.schedules) ? result.schedules : [];
      const outputs = await connection.sendMessagePromise({ type: "smarthome_jobs/outputs" });
      if (generation !== this.connectionGeneration || sequence !== this.refreshSequence || connection !== this.connection) return;
      this.outputs = Array.isArray(outputs.outputs) ? outputs.outputs : [];
      this.defaultAlertOutput = outputs.default_alert_output || "";
      this.snoozeSeconds = outputs.snooze_seconds || 540;
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

  async operate(target, operation, argumentsValue = {}) {
    if (this.stale || !this.connection || this.busyJobs.has(target.id)) return;
    const connection = this.connection;
    const generation = this.connectionGeneration;
    const isCurrent = () => generation === this.connectionGeneration && connection === this.connection;
    this.busyJobs.add(target.id);
    try {
      try {
        await connection.sendMessagePromise({
          type: "smarthome_jobs/operate", request_id: this.requestId(),
          [target.target_type === "schedule" ? "schedule_id" : "job_id"]: target.id,
          expected_revision: target.revision, operation, arguments: argumentsValue,
        });
      } catch (error) {
        if (!isCurrent()) return false;
        try {
          await this.refresh();
          if (isCurrent()) this.showNotice(`Command result: ${this.errorText(error)}. The list was refreshed; the command was not retried.`);
        } catch (refreshError) {
          if (isCurrent()) this.showError(`Command result is unknown (${this.errorText(error)}); refresh failed (${this.errorText(refreshError)}).`);
        }
        return false;
      }
      if (isCurrent()) await this.refresh().catch((error) => this.showError(error));
      return true;
    } finally {
      this.busyJobs.delete(target.id);
      if (isCurrent()) this.render();
    }
  }

  field(parent, key, label, value, type = "text", options = null, state = this.drafts.create) {
    const wrap = document.createElement("label");
    wrap.className = "field";
    this.appendText(wrap, "span", label);
    const input = document.createElement(options ? "select" : "input");
    input.dataset.field = key;
    if (!options) input.type = type;
    if (options) for (const [optionValue, optionLabel] of options) {
      const option = document.createElement("option"); option.value = optionValue; option.textContent = optionLabel; input.append(option);
    }
    input.value = value == null ? "" : String(value);
    input.addEventListener("input", () => { state[key] = input.value; });
    input.addEventListener("change", () => { state[key] = input.value; });
    wrap.append(input); parent.append(wrap); return input;
  }

  async loadOutputs() {
    if (!this.connection || this.stale) return;
    try {
      const result = await this.connection.sendMessagePromise({ type: "smarthome_jobs/outputs" });
      this.outputs = Array.isArray(result.outputs) ? result.outputs : [];
      this.defaultAlertOutput = result.default_alert_output || "";
      this.snoozeSeconds = result.snooze_seconds || 540;
      this.render();
    } catch (error) { this.showError(error); }
  }

  scheduleFromDraft(draft) {
    const kind = draft.scheduleKind;
    if (kind === "relative") return { kind, seconds: Number(draft.seconds) };
    if (kind === "interval") {
      const schedule = { kind, every: Number(draft.every), unit: draft.unit };
      if (draft.anchor) schedule.anchor = draft.anchor;
      if (draft.timezone) schedule.timezone = draft.timezone;
      return schedule;
    }
    const schedule = { kind, time: draft.time };
    if (kind === "once" && draft.date) schedule.date = draft.date;
    if (kind === "weekly") schedule.weekdays = String(draft.weekdays || "0").split(",").map((item) => Number(item.trim()));
    if (kind === "monthly") schedule.day = Number(draft.day);
    if (draft.timezone) schedule.timezone = draft.timezone;
    return schedule;
  }

  async createFromDraft() {
    if (this.stale || !this.connection || this.createBusy) return;
    const draft = this.drafts.create;
    const kind = draft.kind;
    let job;
    if (kind === "timer") {
      const seconds = Number(draft.seconds);
      if (!Number.isInteger(seconds) || seconds < 1) { this.showNotice("Enter a positive whole number of seconds."); return; }
      job = { kind, name: draft.name || "Timer", schedule: { kind: "relative", seconds }, payload: { duration_seconds: seconds } };
    } else {
      const schedule = this.scheduleFromDraft(draft);
      if ((schedule.kind === "relative" && (!Number.isInteger(schedule.seconds) || schedule.seconds < 1)) ||
          (schedule.kind !== "relative" && schedule.kind !== "interval" && !schedule.time) ||
          (schedule.kind === "interval" && (!Number.isInteger(schedule.every) || schedule.every < 1)) ||
          (kind === "reminder" && !draft.message.trim())) {
        this.showNotice("Complete the schedule and required reminder message."); return;
      }
      job = { kind, name: draft.name || kind, schedule };
      if (kind === "reminder") job.payload = { message: draft.message };
    }
    if (draft.output) job.source_satellite = draft.output;
    if (draft.output) {
      const selected = this.outputs.find((item) => item.entity_id === draft.output);
      const supported = kind === "timer" ? selected?.supports_timer && selected.available
        : kind === "reminder" ? selected?.supports_announce : selected?.supports_timer;
      if (!supported) { this.showNotice("The selected output does not support this job type."); return; }
    }
    const generation = this.connectionGeneration;
    this.createBusy = true;
    const submit = this.shadowRoot.querySelector('button[data-create-job="true"]');
    if (submit) submit.disabled = true;
    try {
      await this.connection.sendMessagePromise({ type: "smarthome_jobs/create", request_id: this.requestId(), job });
      if (generation === this.connectionGeneration) { this.drafts.create.name = ""; this.drafts.create.message = ""; await this.refresh(); }
    } catch (error) {
      if (generation !== this.connectionGeneration) return;
      await this.refresh().catch((refreshError) => this.showError(refreshError));
      if (!this.stale) this.showNotice(`Create result: ${this.errorText(error)}. The list was refreshed; the command was not retried.`);
    } finally {
      this.createBusy = false;
      const current = this.shadowRoot.querySelector('button[data-create-job="true"]');
      if (current) current.disabled = this.stale;
    }
  }

  async saveScheduleEdit(schedule) {
    const draft = this.drafts.edit[schedule.id] || {};
    const original = draft.original || {};
    const args = {};
    const revision = draft.revision;
    if (draft.name !== original.name) args.name = draft.name;
    const scheduleChanges = {};
    for (const key of ["time", "date", "weekdays", "day", "every", "unit", "anchor", "timezone"]) {
      if (draft[key] !== original[key] && draft[key] !== undefined && draft[key] !== "") {
        scheduleChanges[key] = key === "weekdays" ? String(draft[key]).split(",").map((item) => Number(item.trim()))
          : ["day", "every"].includes(key) ? Number(draft[key]) : draft[key];
      }
    }
    const kindChanged = draft.scheduleKind !== schedule.schedule?.kind;
    if (Object.keys(scheduleChanges).length || kindChanged) {
      const kind = draft.scheduleKind;
      const timezone = draft.timezone || schedule.schedule?.timezone;
      if (kind === "relative") args.schedule = { kind, seconds: Number(draft.seconds) };
      else if (kind === "interval") args.schedule = { kind, every: Number(draft.every), unit: draft.unit,
        ...(draft.anchor ? { anchor: draft.anchor } : {}), ...(timezone ? { timezone } : {}) };
      else if (kind === "weekly") args.schedule = { kind, time: draft.time,
        weekdays: String(draft.weekdays || "").split(",").map((item) => Number(item.trim())), ...(timezone ? { timezone } : {}) };
      else if (kind === "monthly") args.schedule = { kind, time: draft.time, day: Number(draft.day), ...(timezone ? { timezone } : {}) };
      else if (kind === "once") args.schedule = { kind, date: draft.date, time: draft.time, ...(timezone ? { timezone } : {}) };
      else args.schedule = { kind, time: draft.time, ...(timezone ? { timezone } : {}) };
    }
    if (draft.message !== original.message) args.payload = { message: draft.message };
    if (draft.output && draft.output !== original.output) args.source_satellite = draft.output;
    if (await this.operate({ id: schedule.id, revision: revision ?? schedule.revision, target_type: "schedule" }, "edit", args)) {
      this.editingScheduleId = null;
      delete this.drafts.edit[schedule.id];
      this.render();
    }
  }

  localOnceDateTime(dueAt, timeZone) {
    const date = new Date(dueAt);
    if (Number.isNaN(date.getTime())) return { date: "", time: "" };
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: timeZone || "UTC", year: "numeric", month: "2-digit", day: "2-digit",
    }).formatToParts(date);
    const get = (type) => parts.find((part) => part.type === type)?.value || "";
    const clock = new Intl.DateTimeFormat("en-GB", {
      timeZone: timeZone || "UTC", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    }).format(date);
    return { date: `${get("year")}-${get("month")}-${get("day")}`, time: clock };
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
      .form { border: 1px solid var(--divider-color); border-radius: 10px; padding: 16px; margin: 16px 0; display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 220px), 1fr)); gap: 12px; }
      .field { display: grid; gap: 4px; }
      .field input, .field select { font: inherit; padding: 8px; color: var(--primary-text-color); background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 4px; }
      .form button { align-self: end; }
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

    this.renderCreateForm(main);
    this.renderSchedules(main);

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

  renderCreateForm(main) {
    this.appendText(main, "h2", "Create a job");
    const form = document.createElement("section"); form.className = "form";
    const d = this.drafts.create;
    const kind = this.field(form, "kind", "Type", d.kind, "text", [["timer", "Timer"], ["alarm", "Alarm"], ["reminder", "Reminder"]]);
    kind.addEventListener("change", () => { this.drafts.create.kind = kind.value; if (kind.value === "timer") this.drafts.create.scheduleKind = "relative"; this.render(); });
    this.field(form, "name", "Name", d.name);
    if (d.kind !== "timer") {
      const scheduleKind = this.field(form, "scheduleKind", "Schedule", d.scheduleKind, "text", [["relative", "In a duration"], ["once", "Once at a date and time"], ["daily", "Daily"], ["weekly", "Weekly"], ["monthly", "Monthly"], ["interval", "Every interval"]]);
      scheduleKind.addEventListener("change", () => { this.drafts.create.scheduleKind = scheduleKind.value; this.render(); });
    }
    if (d.scheduleKind === "relative" || d.kind === "timer") this.field(form, "seconds", "Seconds", d.seconds, "number");
    if (d.scheduleKind !== "relative" && d.kind !== "timer") this.field(form, "time", "Local time (HH:MM)", d.time, "time");
    if (d.scheduleKind === "once") this.field(form, "date", "Local date (optional)", d.date, "date");
    if (d.scheduleKind === "weekly") this.field(form, "weekdays", "Weekdays (0 Monday … 6 Sunday)", d.weekdays);
    if (d.scheduleKind === "monthly") this.field(form, "day", "Day of month", d.day, "number");
    if (d.scheduleKind === "interval") {
      this.field(form, "every", "Every", d.every, "number");
      this.field(form, "unit", "Unit", d.unit, "text", [["minutes", "Minutes"], ["hours", "Hours"], ["days", "Days"], ["weeks", "Weeks"]]);
    }
    if (d.kind === "reminder") this.field(form, "message", "Reminder message", d.message);
    const outputOptions = [["", this.defaultAlertOutput ? `HA default (${this.defaultAlertOutput})` : "HA default output"],
      ...this.outputs.filter((item) => d.kind === "timer" ? item.supports_timer && item.available : d.kind === "reminder" ? item.supports_announce : item.supports_timer)
        .map((item) => [item.entity_id, `${item.name}${item.available ? "" : " (offline)"} — ${item.area_name || "No area"}`])];
    if (d.output && !outputOptions.some(([id]) => id === d.output)) outputOptions.push([d.output, `Selected output needs review (${d.output})`]);
    this.field(form, "output", "Output", d.output, "text", outputOptions);
    if (d.kind !== "timer" && d.scheduleKind !== "relative") this.field(form, "timezone", "IANA timezone (optional)", d.timezone, "text");
    if (d.scheduleKind === "interval") this.field(form, "anchor", "Anchor (optional local date and time)", d.anchor, "text");
    const create = document.createElement("button"); create.type = "button"; create.dataset.createJob = "true"; create.textContent = "Create"; create.disabled = this.stale || this.createBusy;
    create.addEventListener("click", () => this.createFromDraft()); form.append(create);
    main.append(form);
    if (!this.outputs.length && !this.stale) this.appendText(main, "p", "No registered outputs are available; the HA default can be used when configured.", "meta");
  }

  renderSchedules(main) {
    this.appendText(main, "h2", "Schedule definitions");
    if (!this.schedules.length) { this.appendText(main, "p", "No alarm or reminder definitions.", "empty"); return; }
    const group = document.createElement("section"); group.className = "group";
    for (const schedule of this.schedules) {
      const card = document.createElement("article"); card.className = "job"; card.dataset.scheduleId = schedule.id;
      this.appendText(card, "h3", schedule.name || this.label(schedule.kind));
      const current = schedule.current_job || this.jobs.find((job) => job.id === schedule.current_job_id) || {};
      this.appendText(card, "p", `${this.label(schedule.kind)} · ${schedule.enabled ? "Enabled" : "Disabled"} · ${this.date(current.due_at || schedule.next_due_at || schedule.due_at)}`, "meta");
      if (schedule.kind === "reminder" && schedule.payload?.message) this.appendText(card, "p", schedule.payload.message, "meta");
      const delivery = current.delivery_guarantee || schedule.delivery_guarantee;
      if (delivery) this.appendText(card, "p", `Delivery: ${this.label(delivery)}`, "meta");
      if (schedule.source_satellite) this.appendText(card, "p", `Output: ${schedule.source_satellite}`, "meta");
      const actions = document.createElement("div"); actions.className = "actions";
      if (schedule.allowed_operations?.includes("edit")) {
        const edit = document.createElement("button"); edit.type = "button"; edit.textContent = "Edit";
        edit.disabled = this.stale || this.busyJobs.has(schedule.id);
        edit.addEventListener("click", () => {
          if (this.editingScheduleId === schedule.id) delete this.drafts.edit[schedule.id];
          this.editingScheduleId = this.editingScheduleId === schedule.id ? null : schedule.id;
          this.render();
        });
        actions.append(edit);
      }
      if (schedule.allowed_operations?.includes("disable")) this.addTargetButton(actions, "Disable", schedule, "disable", "schedule");
      if (schedule.allowed_operations?.includes("enable")) this.addTargetButton(actions, "Enable", schedule, "enable", "schedule");
      if (schedule.allowed_operations?.includes("cancel")) this.addTargetButton(actions, "Cancel definition", schedule, "cancel", "schedule");
      if (actions.childElementCount) card.append(actions);
      if (this.editingScheduleId === schedule.id) {
        const onceTime = schedule.schedule?.kind === "once"
          ? this.localOnceDateTime(schedule.schedule.due_at || current.due_at, schedule.schedule.timezone) : {};
        const draft = this.drafts.edit[schedule.id] ||= {
          revision: schedule.revision, name: schedule.name || "", time: schedule.schedule?.time || onceTime.time || "",
          date: schedule.schedule?.date || onceTime.date || "", weekdays: (schedule.schedule?.weekdays || []).join(","),
          day: schedule.schedule?.day || "", every: schedule.schedule?.every || "", unit: schedule.schedule?.unit || "",
          anchor: schedule.schedule?.anchor || "", timezone: schedule.schedule?.timezone || "",
          seconds: schedule.schedule?.seconds || "", scheduleKind: schedule.schedule?.kind || "once",
          message: schedule.payload?.message || "", output: schedule.source_satellite || "",
        };
        draft.original ||= { name: schedule.name || "", time: schedule.schedule?.time || onceTime.time || "",
          date: schedule.schedule?.date || onceTime.date || "", weekdays: (schedule.schedule?.weekdays || []).join(","),
          day: schedule.schedule?.day || "", every: schedule.schedule?.every || "", unit: schedule.schedule?.unit || "",
          anchor: schedule.schedule?.anchor || "", timezone: schedule.schedule?.timezone || "",
          seconds: schedule.schedule?.seconds || "", scheduleKind: schedule.schedule?.kind || "once",
          message: schedule.payload?.message || "", output: schedule.source_satellite || "" };
        const form = document.createElement("section"); form.className = "form";
        this.field(form, "name", "Name", draft.name, "text", null, draft);
        const kindField = this.field(form, "scheduleKind", "Schedule", draft.scheduleKind, "text", [["relative", "In a duration"], ["once", "Once"], ["daily", "Daily"], ["weekly", "Weekly"], ["monthly", "Monthly"], ["interval", "Every interval"]], draft);
        kindField.addEventListener("change", () => { draft.scheduleKind = kindField.value; this.render(); });
        if (draft.scheduleKind === "relative") this.field(form, "seconds", "Seconds", draft.seconds, "number", null, draft);
        if (draft.scheduleKind !== "relative" && draft.scheduleKind !== "interval") this.field(form, "time", "Local time", draft.time, "time", null, draft);
        if (draft.scheduleKind === "once") this.field(form, "date", "Local date", draft.date, "date", null, draft);
        if (draft.scheduleKind === "weekly") this.field(form, "weekdays", "Weekdays (0 Monday … 6 Sunday)", draft.weekdays, "text", null, draft);
        if (draft.scheduleKind === "monthly") this.field(form, "day", "Day of month", draft.day, "number", null, draft);
        if (draft.scheduleKind === "interval") {
          this.field(form, "every", "Every", draft.every, "number", null, draft);
          this.field(form, "unit", "Unit", draft.unit, "text", [["minutes", "Minutes"], ["hours", "Hours"], ["days", "Days"], ["weeks", "Weeks"]], draft);
          this.field(form, "anchor", "Anchor (optional local date and time)", draft.anchor, "text", null, draft);
        }
        this.field(form, "timezone", "IANA timezone (optional)", draft.timezone, "text", null, draft);
        if (schedule.kind === "reminder") this.field(form, "message", "Reminder message", draft.message, "text", null, draft);
        const outputOptions = [[draft.output, draft.output ? `Keep bound output (${draft.output})` : "Keep HA default"], ...this.outputs
          .filter((item) => schedule.kind === "reminder" ? item.supports_announce : item.supports_timer)
          .map((item) => [item.entity_id, `${item.name}${item.available ? "" : " (offline)"}`])];
        this.field(form, "output", "Output", draft.output, "text", outputOptions, draft);
        const save = document.createElement("button"); save.type = "button"; save.textContent = "Save changes";
        save.disabled = this.stale; save.addEventListener("click", () => this.saveScheduleEdit(schedule)); form.append(save);
        const reload = document.createElement("button"); reload.type = "button"; reload.textContent = "Discard draft and reload latest";
        reload.disabled = this.stale; reload.addEventListener("click", () => { delete this.drafts.edit[schedule.id]; this.render(); }); form.append(reload);
        const close = document.createElement("button"); close.type = "button"; close.textContent = "Cancel edit";
        close.addEventListener("click", () => { delete this.drafts.edit[schedule.id]; this.editingScheduleId = null; this.render(); }); form.append(close);
        card.append(form);
      }
      group.append(card);
    }
    main.append(group);
  }

  addTargetButton(parent, title, record, operation, targetType) {
    const button = document.createElement("button"); button.type = "button"; button.textContent = title;
    button.disabled = this.stale || this.busyJobs.has(record.id);
    button.addEventListener("click", () => this.operate({ id: record.id, revision: record.revision, target_type: targetType }, operation));
    parent.append(button);
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
    if (job.delivery_guarantee) this.appendText(card, "p", `Delivery: ${this.label(job.delivery_guarantee)}`, "meta");
    if (job.status === "pending" || job.status === "due") {
      const countdown = this.appendText(card, "p", this.remaining(job.due_at), "meta");
      countdown.dataset.countdown = job.id;
    }
    if (job.status === "paused") {
      this.appendText(card, "p", `${job.remaining_seconds ?? job.paused_remaining_seconds} seconds remaining (paused)`, "meta");
    }
    if (job.kind === "alarm" && job.delivery_guarantee === "handoff_only") {
      this.appendText(card, "p", "This output does not acknowledge playback or stopping. A stop request cannot confirm silence.", "meta");
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
    if (job.allowed_operations?.includes("cancel")) this.addButton(actions, "Cancel", job, "cancel");
    for (const [operation, title] of [["pause", "Pause"], ["resume", "Resume"], ["adjust_remaining", "Add/subtract time"], ["set_remaining", "Set remaining"], ["rename", "Rename"], ["restart", "Restart"]]) {
      if (job.allowed_operations?.includes(operation)) this.addOperationButton(actions, title, job, operation);
    }
    if (job.allowed_operations?.includes("dismiss")) {
      if (job.kind === "timer") this.addButton(actions, "Dismiss timer", job, "timer_dismiss");
      else this.addOperationButton(actions, "Dismiss alarm", job, "dismiss");
    }
    if (job.allowed_operations?.includes("snooze")) this.addOperationButton(actions, "Snooze", job, "snooze");
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

  addOperationButton(parent, title, job, operation) {
    const button = document.createElement("button"); button.type = "button"; button.textContent = title;
    button.dataset.jobId = job.id;
    button.disabled = this.stale || this.busyJobs.has(job.id);
    button.addEventListener("click", async () => {
      let argumentsValue = {};
      if (["adjust_remaining", "set_remaining", "snooze"].includes(operation)) {
        const raw = window.prompt(operation === "adjust_remaining" ? "Seconds to add (negative subtracts):" : "Seconds:", operation === "snooze" ? String(this.snoozeSeconds) : "60");
        if (raw === null) return;
        const seconds = Number(raw);
        if (!Number.isInteger(seconds)) { this.showNotice("Enter a whole number of seconds."); return; }
        argumentsValue = operation === "adjust_remaining" ? { delta_seconds: seconds } : { seconds };
      } else if (operation === "rename") {
        const name = window.prompt("New timer name:", job.name || ""); if (name === null) return; argumentsValue = { name };
      }
      await this.operate({ id: job.id, revision: job.revision, target_type: "job" }, operation, argumentsValue);
    });
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
