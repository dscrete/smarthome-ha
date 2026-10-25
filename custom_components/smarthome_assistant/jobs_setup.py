"""Home Assistant persistence and event scheduling for SmartHome jobs."""
from __future__ import annotations

from pathlib import Path

from homeassistant.components import persistent_notification
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.panel_custom import async_register_panel
from homeassistant.components.frontend import async_remove_panel
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.helpers.event import async_track_point_in_utc_time

from .jobs import DOMAIN, JobsManager, STORAGE_VERSION
from .timer_delivery import configure_delivery
from .job_websocket import async_register_websocket_commands
from .reminder_delivery import ReminderDelivery

async def async_setup_jobs(hass):
    """Load persistent jobs and register authenticated WebSocket commands."""
    if DOMAIN in hass.data:
        if not getattr(hass.data[DOMAIN], "_unified_integration", False):
            raise RuntimeError("Remove the legacy smarthome_jobs YAML configuration and restart HA; stored jobs are preserved")
        return True

    def notify(job, reason):
        labels = {
            "missed": "A scheduled action was missed. It was not run.",
            "unknown": "A scheduled action may have run, but its result is unknown. It will not be repeated.",
            "failed": "A scheduled action failed.",
            "undelivered": "A timer expired without being delivered to its satellite.",
            "native_unknown": "A timer alert may have reached its satellite, but delivery or dismissal could not be confirmed. It will not be replayed.",
            "alarm_missed": "An alarm was missed because it could not be delivered within five minutes.",
            "alarm_unknown": "An alarm may have reached its satellite, but delivery or dismissal could not be confirmed. It will not be replayed.",
            "reminder_unknown": "A reminder announcement may have reached its satellite, but delivery could not be confirmed. It will not be replayed.",
        }
        name = f"Timer {job['name']} expired without delivery." if reason == "undelivered" and job.get("name") else labels[reason]
        persistent_notification.async_create(
            hass,
            name,
            title="SmartHome job update",
            notification_id=f"{DOMAIN}_{job['id']}_{reason}",
        )

    store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.storage")
    manager = JobsManager(
        hass,
        store,
        now=dt_util.utcnow,
        # HA runs unmarked synchronous handlers in its executor. These handlers
        # enqueue async jobs and must run on HA's event loop instead.
        schedule_at=lambda handler, when: async_track_point_in_utc_time(hass, callback(handler), when),
        notify=notify,
        clear_notice=lambda job: persistent_notification.async_dismiss(hass, f"{DOMAIN}_{job['id']}_undelivered"),
    )
    await manager.async_load()
    manager._unified_integration = True
    hass.data[DOMAIN] = manager
    delivery = configure_delivery(hass, manager, {})
    manager.delivery_worker = delivery
    manager.reminder_worker = ReminderDelivery(manager, delivery.standard.output)
    async_register_websocket_commands(hass)
    legacy_timers = [job["id"] for job in manager.jobs.values()
        if job["kind"] == "timer" and job["status"] in {"pending", "paused", "due", "sounding"}
        and job.get("delivery", {}).get("owner") != "ha"]
    if legacy_timers:
        persistent_notification.async_create(hass,
            "Finish or cancel these Gateway-owned timers before upgrading to the HA-only "
            "SmartHome Core. Their delivery ownership was not changed: " + ", ".join(legacy_timers),
            title="Review legacy SmartHome timers", notification_id=f"{DOMAIN}_gateway_retirement")
    else:
        persistent_notification.async_dismiss(hass, f"{DOMAIN}_gateway_retirement")

    @callback
    def start_delivery(_event):
        manager.delivery_worker.start()
        manager.reminder_worker.start()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, start_delivery)
    if getattr(hass, "is_running", False):
        manager.delivery_worker.start()
        manager.reminder_worker.start()

    @callback
    def stop_delivery(_event):
        hass.async_create_task(manager.delivery_worker.stop())
        hass.async_create_task(manager.reminder_worker.stop())

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_delivery)
    return True


async def async_configure_panel(hass, enabled):
    """Install or remove this integration's optional sidebar panel."""
    frontend = hass.data.setdefault(f"{DOMAIN}_frontend", {})
    if not frontend.get("static_registered"):
        if enabled:
            frontend_path = Path(__file__).parent / "www" / "smarthome-jobs.js"
            await hass.http.async_register_static_paths([
                StaticPathConfig("/smarthome_assistant/smarthome-jobs.js", str(frontend_path), False)
            ])
            frontend["static_registered"] = True
    if enabled and not frontend.get("panel_registered"):
        await async_register_panel(
            hass,
            frontend_url_path="smarthome",
            webcomponent_name="smarthome-jobs-panel",
            sidebar_title="SmartHome",
            sidebar_icon="mdi:calendar-clock",
            module_url="/smarthome_assistant/smarthome-jobs.js",
            require_admin=True,
        )
        frontend["panel_registered"] = True
    elif not enabled and frontend.get("panel_registered"):
        async_remove_panel(hass, "smarthome")
        frontend["panel_registered"] = False


async def async_configure_outputs(hass, bindings):
    """Apply UI bindings without restarting the durable job manager.

    A conversation reload must not reset delivery ownership, sequence numbers,
    callbacks or receipt acknowledgement state. Binding removal/changes wait
    until the affected accepted timers are terminal.
    """
    manager = hass.data[DOMAIN]
    old = manager.delivery_worker
    if old.bindings == bindings:
        return
    await manager.async_validate_output_bindings(bindings)
    # Validate before stopping the existing worker.
    replacement = configure_delivery(hass, manager, bindings)
    await old.stop()
    manager.delivery_worker = replacement
    if getattr(hass, "is_running", False):
        replacement.start()
