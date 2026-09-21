/*
 * climate-controller-schedule-card
 *
 * A 24x7 presence grid for the climate_controller integration: hours across,
 * weekdays down, one cell per hour of the week. Cells are coloured by where
 * their value came from — blue for what the motion sensors learned, orange for
 * what the user set by hand, grey for an hour that saw motion but stayed under
 * the threshold. Clicking (or dragging a rectangle) paints cells through the
 * `climate_controller.set_occupancy_slot` service.
 *
 * Plain custom element on purpose: no build step, no Lit import, nothing to
 * keep in sync with whatever the frontend ships this month.
 *
 * Install: copy to /config/www/climate-controller/, add it under
 * Settings -> Dashboards -> Resources as
 *   /local/climate-controller/climate-controller-schedule-card.js  (module)
 * then use:
 *   type: custom:climate-controller-schedule-card
 *   entity: climate.your_controller
 */

const CELL_INACTIVE = 0;
const CELL_AUTO = 1;
const CELL_MANUAL_ON = 2;
const CELL_MANUAL_OFF = 3;
const CELL_SEEN = 4;

const HOURS = 24;
const DAYS = 7;

const CELL_CLASS = {
  [CELL_INACTIVE]: "c-inactive",
  [CELL_AUTO]: "c-auto",
  [CELL_MANUAL_ON]: "c-manual-on",
  [CELL_MANUAL_OFF]: "c-manual-off",
  [CELL_SEEN]: "c-seen",
};

// What a click turns a cell into. A cell the user has not touched flips to the
// opposite of what learning says; a cell they have touched cycles back to auto,
// so two clicks always get you back to where you started.
const NEXT_MODE = {
  [CELL_INACTIVE]: "active",
  [CELL_SEEN]: "active",
  [CELL_AUTO]: "inactive",
  [CELL_MANUAL_ON]: "auto",
  [CELL_MANUAL_OFF]: "auto",
};

const STYLES = `
  :host {
    /* Learned and hand-set cells are told apart by hue, not by shade, so the
       distinction survives both themes and the usual forms of colour blindness
       (blue vs orange is the one pair that does). Every value can be
       overridden from a theme. */
    --cc-auto: var(--climate-controller-auto-color, #2f7ed8);
    --cc-manual: var(--climate-controller-manual-color, #ef7c22);
    --cc-seen: var(--climate-controller-seen-color, #9aa0a6);
    --cc-empty: var(--climate-controller-empty-color, var(--divider-color, #e0e0e0));
    --cc-preheat: var(--climate-controller-preheat-color, rgba(47, 126, 216, .32));
    --cc-preheat-chip: var(--climate-controller-preheat-chip-color, #b06000);
  }
  ha-card { padding: 12px 16px 16px; }
  .head {
    display: flex; align-items: baseline; gap: 8px;
    flex-wrap: wrap; margin-bottom: 10px;
  }
  .title { font-size: 1.1em; font-weight: 500; }
  .state {
    font-size: .85em; padding: 2px 8px; border-radius: 10px;
    background: var(--secondary-background-color);
    color: var(--secondary-text-color);
  }
  .state.expected { background: var(--cc-auto); color: #fff; }
  .state.preheat  { background: var(--cc-preheat-chip); color: #fff; }
  .grid {
    display: grid;
    grid-template-columns: auto repeat(${HOURS}, minmax(0, 1fr));
    gap: 2px;
    user-select: none;
    touch-action: none;
  }
  .hlabel, .dlabel {
    font-size: .68em; color: var(--secondary-text-color);
    line-height: 1; text-align: center;
  }
  .hlabel { padding-bottom: 3px; }
  .dlabel { padding-right: 6px; text-align: right; align-self: center; }
  .cell {
    aspect-ratio: 1 / 1.35;
    min-height: 14px;
    border-radius: 3px;
    background: var(--cc-empty);
    border: 1px solid transparent;
    cursor: pointer;
  }
  .cell:hover { filter: brightness(1.15); }
  .c-auto      { background: var(--cc-auto); }
  .c-manual-on { background: var(--cc-manual); }
  .c-seen      { background: var(--cc-seen); }
  .c-manual-off {
    background: var(--cc-empty);
    border: 1px dashed var(--cc-manual);
  }
  /* Warming up for a slot that has not started yet: same hue, dimmed. */
  .preheat { background: var(--cc-preheat); }
  .now { outline: 2px solid var(--primary-text-color); outline-offset: 1px; }
  .picking { filter: brightness(1.35); }
  .legend {
    display: flex; flex-wrap: wrap; gap: 12px;
    margin-top: 12px; font-size: .78em; color: var(--secondary-text-color);
  }
  .legend span { display: inline-flex; align-items: center; gap: 5px; }
  .swatch {
    width: 11px; height: 11px; border-radius: 3px;
    display: inline-block; background: var(--cc-empty);
  }
  .footer {
    margin-top: 8px; font-size: .78em; color: var(--secondary-text-color);
  }
  .notice { padding: 8px 0; color: var(--secondary-text-color); }
`;

class ClimateControllerScheduleCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._built = false;
    this._drag = null;
  }

  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error("climate-controller-schedule-card: `entity` is required");
    }
    if (!config.entity.startsWith("climate.")) {
      throw new Error(
        "climate-controller-schedule-card: `entity` must be a climate entity"
      );
    }
    this._config = config;
    this._built = false;
    this.shadowRoot.innerHTML = "";
  }

  static getStubConfig(hass) {
    const candidate = Object.keys(hass.states || {}).find(
      (id) =>
        id.startsWith("climate.") &&
        hass.states[id].attributes.occupancy_grid !== undefined
    );
    return { entity: candidate || "climate.climate_controller" };
  }

  getCardSize() {
    return 6;
  }

  set hass(hass) {
    this._hass = hass;
    // Repainting mid-drag would swap the DOM under the pointer and lose the
    // selection, and the state we would paint is the pre-click one anyway.
    if (this._drag) return;
    this._render();
  }

  // ---- rendering ---------------------------------------------------------

  _stateObj() {
    return this._hass && this._hass.states
      ? this._hass.states[this._config.entity]
      : undefined;
  }

  _dayNames() {
    const lang =
      (this._hass && this._hass.locale && this._hass.locale.language) ||
      navigator.language ||
      "en";
    // Cached: this is called once per cell while building tooltips, and a
    // fresh Intl.DateTimeFormat 168 times a repaint is not free.
    if (!this._dayNameCache || this._dayNameCache.lang !== lang) {
      const format = new Intl.DateTimeFormat(lang, {
        weekday: "short",
        timeZone: "UTC",
      });
      this._dayNameCache = {
        lang,
        // 2024-01-01 was a Monday, and weekday 0 is Monday in the grid.
        names: Array.from({ length: DAYS }, (_, day) =>
          format.format(new Date(Date.UTC(2024, 0, 1 + day)))
        ),
      };
    }
    return this._dayNameCache.names;
  }

  _render() {
    const stateObj = this._stateObj();
    if (!stateObj) {
      this._renderNotice(`Entity ${this._config.entity} not found`);
      return;
    }
    const grid = stateObj.attributes.occupancy_grid;
    if (!Array.isArray(grid) || grid.length !== DAYS) {
      this._renderNotice(
        "This entity publishes no occupancy grid. Configure the occupancy " +
          "schedule in the integration options first."
      );
      return;
    }

    if (!this._built) this._build();
    this._paint(stateObj, grid);
  }

  _renderNotice(text) {
    this._built = false;
    this.shadowRoot.innerHTML = `
      <style>${STYLES}</style>
      <ha-card><div class="notice">${text}</div></ha-card>`;
  }

  _build() {
    const dayNames = this._dayNames();
    const parts = [`<style>${STYLES}</style>`, `<ha-card>`];
    parts.push(`<div class="head">
        <span class="title"></span>
        <span class="state"></span>
      </div>`);
    parts.push(`<div class="grid">`);
    parts.push(`<div></div>`);
    for (let hour = 0; hour < HOURS; hour++) {
      // Every hour is a column, but only every third gets a label — 24 numbers
      // in a row on a phone is a grey smear.
      parts.push(
        `<div class="hlabel">${hour % 3 === 0 ? hour : "&nbsp;"}</div>`
      );
    }
    for (let day = 0; day < DAYS; day++) {
      parts.push(`<div class="dlabel">${dayNames[day]}</div>`);
      for (let hour = 0; hour < HOURS; hour++) {
        parts.push(`<div class="cell" data-day="${day}" data-hour="${hour}"></div>`);
      }
    }
    parts.push(`</div>`);
    parts.push(`<div class="legend">
        <span><i class="swatch c-auto"></i>learned</span>
        <span><i class="swatch c-manual-on"></i>set by hand</span>
        <span><i class="swatch c-manual-off"></i>forced off</span>
        <span><i class="swatch c-seen"></i>below threshold</span>
        <span><i class="swatch preheat"></i>preheat</span>
      </div>`);
    parts.push(`<div class="footer"></div>`);
    parts.push(`</ha-card>`);
    this.shadowRoot.innerHTML = parts.join("");

    // Cell lookups happen 168 times a repaint; resolve them once.
    this._cells = Array.from({ length: DAYS }, (_, day) =>
      Array.from({ length: HOURS }, (_, hour) =>
        this.shadowRoot.querySelector(
          `.cell[data-day="${day}"][data-hour="${hour}"]`
        )
      )
    );

    const gridEl = this.shadowRoot.querySelector(".grid");
    gridEl.addEventListener("pointerdown", (ev) => this._onPointerDown(ev));
    gridEl.addEventListener("pointermove", (ev) => this._onPointerMove(ev));
    // Ending the drag outside the grid still has to commit it.
    this._onPointerUp = () => this._commitDrag();
    window.addEventListener("pointerup", this._onPointerUp);
    window.addEventListener("pointercancel", this._onPointerUp);
    this._built = true;
  }

  disconnectedCallback() {
    if (this._onPointerUp) {
      window.removeEventListener("pointerup", this._onPointerUp);
      window.removeEventListener("pointercancel", this._onPointerUp);
    }
  }

  _paint(stateObj, grid) {
    const attrs = stateObj.attributes;
    const scores = attrs.occupancy_scores || [];
    const preheatMinutes = Number(attrs.occupancy_preheat_minutes || 0);
    const preheatHours = Math.ceil(preheatMinutes / 60);
    const weeks = attrs.occupancy_learning_weeks;

    const root = this.shadowRoot;
    root.querySelector(".title").textContent =
      this._config.title || attrs.friendly_name || "Occupancy";

    const stateEl = root.querySelector(".state");
    const occupancyState = attrs.occupancy_state || "—";
    stateEl.textContent = attrs.occupancy_enabled
      ? occupancyState
      : "schedule off";
    stateEl.className = `state ${attrs.occupancy_enabled ? occupancyState : ""}`;

    const now = new Date();
    // JS Sunday is 0; the grid starts on Monday.
    const nowDay = (now.getDay() + 6) % 7;
    const nowHour = now.getHours();

    const preheatCells = this._preheatCells(grid, preheatHours);

    for (let day = 0; day < DAYS; day++) {
      for (let hour = 0; hour < HOURS; hour++) {
        const cell = this._cells[day][hour];
        if (!cell) continue;
        const value = grid[day][hour];
        const classes = ["cell", CELL_CLASS[value] || "c-inactive"];
        if (preheatCells.has(`${day}:${hour}`)) classes.push("preheat");
        if (day === nowDay && hour === nowHour) classes.push("now");
        cell.className = classes.join(" ");
        cell.title = this._tooltip(day, hour, value, scores, weeks);
      }
    }

    const days = attrs.occupancy_learning_days;
    const minDays = attrs.occupancy_learning_min_days;
    const bits = [];
    if (days !== undefined) {
      bits.push(
        days < minDays
          ? `learning: ${days}/${minDays} days before learned cells count`
          : `learned from ${days} day(s)`
      );
    }
    if (attrs.occupancy_threshold !== undefined) {
      bits.push(`threshold ${attrs.occupancy_threshold} over ${weeks} week(s)`);
    }
    if (preheatMinutes) bits.push(`preheat ${preheatMinutes} min`);
    root.querySelector(".footer").textContent = bits.join(" · ");
  }

  _preheatCells(grid, preheatHours) {
    const cells = new Set();
    if (preheatHours <= 0) return cells;
    for (let day = 0; day < DAYS; day++) {
      for (let hour = 0; hour < HOURS; hour++) {
        const value = grid[day][hour];
        if (value !== CELL_AUTO && value !== CELL_MANUAL_ON) continue;
        // Walk backwards from an active slot, wrapping across midnight and
        // into the previous day, exactly like the integration's lookahead.
        for (let back = 1; back <= preheatHours; back++) {
          const absolute = day * HOURS + hour - back;
          const slot = ((absolute % (DAYS * HOURS)) + DAYS * HOURS) % (DAYS * HOURS);
          const prevDay = Math.floor(slot / HOURS);
          const prevHour = slot % HOURS;
          const prevValue = grid[prevDay][prevHour];
          if (prevValue === CELL_AUTO || prevValue === CELL_MANUAL_ON) continue;
          cells.add(`${prevDay}:${prevHour}`);
        }
      }
    }
    return cells;
  }

  _tooltip(day, hour, value, scores, weeks) {
    const dayName = this._dayNames()[day];
    const label = `${dayName} ${String(hour).padStart(2, "0")}:00`;
    const score =
      Array.isArray(scores) && scores[day] !== undefined
        ? scores[day][hour]
        : undefined;
    const parts = [label];
    if (score !== undefined && score >= 0) {
      parts.push(`${(score / 100).toFixed(2)} of ${weeks} week(s)`);
    } else if (score === -1) {
      parts.push("never observed");
    }
    if (value === CELL_MANUAL_ON) parts.push("forced on");
    else if (value === CELL_MANUAL_OFF) parts.push("forced off");
    else if (value === CELL_AUTO) parts.push("learned");
    else if (value === CELL_SEEN) parts.push("below threshold");
    return parts.join(" · ");
  }

  // ---- painting cells ----------------------------------------------------

  _cellFromEvent(ev) {
    // Hit-test by coordinates rather than reading ev.target: on touch the
    // pointer is captured by the cell the gesture started on, so every
    // subsequent move would otherwise report that same cell and drag would
    // only ever select one square.
    const target = this.shadowRoot.elementFromPoint(ev.clientX, ev.clientY);
    if (!target || !target.classList || !target.classList.contains("cell")) {
      return null;
    }
    return {
      day: Number(target.dataset.day),
      hour: Number(target.dataset.hour),
    };
  }

  _onPointerDown(ev) {
    const cell = this._cellFromEvent(ev);
    if (!cell) return;
    const stateObj = this._stateObj();
    if (!stateObj) return;
    const grid = stateObj.attributes.occupancy_grid;
    const value = grid[cell.day][cell.hour];
    // The whole rectangle gets the mode the anchor cell would have flipped to,
    // so a drag reads as "make all of this look like that first cell".
    this._drag = { anchor: cell, mode: NEXT_MODE[value] || "auto" };
    ev.preventDefault();
    this._highlight(cell);
  }

  _onPointerMove(ev) {
    if (!this._drag) return;
    const cell = this._cellFromEvent(ev);
    if (!cell) return;
    this._drag.last = cell;
    this._highlight(cell);
  }

  _highlight(cell) {
    const { days, hours } = this._rectangle(cell);
    this.shadowRoot.querySelectorAll(".cell").forEach((el) => {
      const inRect =
        days.includes(Number(el.dataset.day)) &&
        hours.includes(Number(el.dataset.hour));
      el.classList.toggle("picking", inRect);
    });
  }

  _rectangle(cell) {
    const anchor = this._drag.anchor;
    const range = (a, b) => {
      const [lo, hi] = a <= b ? [a, b] : [b, a];
      return Array.from({ length: hi - lo + 1 }, (_, i) => lo + i);
    };
    return {
      days: range(anchor.day, cell.day),
      hours: range(anchor.hour, cell.hour),
    };
  }

  _commitDrag() {
    const drag = this._drag;
    if (!drag) return;
    const { days, hours } = this._rectangle(drag.last || drag.anchor);
    this._drag = null;
    this.shadowRoot
      .querySelectorAll(".cell.picking")
      .forEach((el) => el.classList.remove("picking"));

    this._hass.callService("climate_controller", "set_occupancy_slot", {
      entity_id: this._config.entity,
      day: days,
      hour: hours,
      mode: drag.mode,
    });
  }
}

customElements.define(
  "climate-controller-schedule-card",
  ClimateControllerScheduleCard
);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "climate-controller-schedule-card",
  name: "Climate Controller Schedule",
  description:
    "24x7 presence grid learned from motion sensors, editable by hand.",
  preview: false,
  documentationURL: "https://github.com/smart-ha/climate-controller",
});
